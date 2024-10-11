#!/usr/bin/env python

import glob
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from argparse import ArgumentParser
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import HTTPRedirectHandler as UrllibHTTPRedirectHandler
from urllib.request import Request, build_opener, install_opener, urlopen

import setuptools  # noqa: F401 not used in code but needed in runtime, don't remove!
from scrapy.utils.conf import closest_scrapy_cfg
from scrapy.utils.project import inside_project
from urllib3.filepost import encode_multipart_formdata
from w3lib.http import basic_auth_header

from scrapyd_client.utils import get_auth, get_config

_SETUP_PY_TEMPLATE = """
# Automatically created by: scrapyd-deploy

from setuptools import setup, find_packages

setup(
    name         = 'project',
    version      = '1.0',
    packages     = find_packages(),
    entry_points = {'scrapy': ['settings = %(settings)s']},
)
""".lstrip()


def parse_args():
    parser = ArgumentParser(description="Deploy Scrapy project to Scrapyd server")
    parser.add_argument("target", nargs="?", default="default", metavar="TARGET")
    parser.add_argument("-p", "--project", help="the project name in the TARGET")
    parser.add_argument(
        "-v", "--version", help="the version to deploy. Defaults to current timestamp"
    )
    parser.add_argument(
        "-l", "--list-targets", action="store_true", help="list available targets"
    )
    parser.add_argument(
        "-a", "--deploy-all-targets", action="store_true", help="deploy all targets"
    )
    parser.add_argument(
        "-d",
        "--debug",
        action="store_true",
        help="debug mode (do not remove build dir)",
    )
    parser.add_argument(
        "-L",
        "--list-projects",
        metavar="TARGET",
        help="list available projects in the TARGET",
    )
    parser.add_argument(
        "--egg", metavar="FILE", help="use the given egg, instead of building it"
    )
    parser.add_argument(
        "--build-egg", metavar="FILE", help="only build the egg, don't deploy it"
    )
    parser.add_argument(
        "--include-dependencies",
        action="store_true",
        help="include dependencies from requirements.txt in the egg",
    )
    return parser.parse_args()


def main():
    opts = parse_args()
    exitcode = 0
    if not inside_project():
        _log("Error: no Scrapy project found in this location")
        sys.exit(1)

    install_opener(build_opener(HTTPRedirectHandler))

    if opts.list_targets:
        for name, target in _get_targets().items():
            print("%-20s %s" % (name, target["url"]))
        return

    if opts.list_projects:
        target = _get_target(opts.list_projects)
        request = Request(_url(target, "listprojects.json"))
        _add_auth_header(request, target)
        response = urlopen(request)
        projects = json.loads(response.read())["projects"]
        print(os.linesep.join(projects))
        return

    tmpdir = None

    if opts.build_egg:  # build egg only
        eggpath, tmpdir = _build_egg(opts)
        _log("Writing egg to %s" % opts.build_egg)
        shutil.copyfile(eggpath, opts.build_egg)
    elif opts.deploy_all_targets:
        version = None
        for name, target in _get_targets().items():
            if version is None:
                version = _get_version(target, opts)
            _, tmpdir = _build_egg_and_deploy_target(target, version, opts)
            _remove_tmpdir(tmpdir, opts)
    else:  # buld egg and deploy
        target = _get_target(opts.target)
        version = _get_version(target, opts)
        exitcode, tmpdir = _build_egg_and_deploy_target(target, version, opts)
        _remove_tmpdir(tmpdir, opts)

    sys.exit(exitcode)


def _remove_tmpdir(tmpdir, opts):
    if tmpdir:
        if opts.debug:
            _log("Output dir not removed: %s" % tmpdir)
        else:
            shutil.rmtree(tmpdir)


def _build_egg_and_deploy_target(target, version, opts):
    exitcode = 0
    tmpdir = None

    project = opts.project or target.get("project")
    if not project:
        raise _fail("Error: Missing project")

    if opts.egg:
        _log("Using egg: %s" % opts.egg)
        eggpath = opts.egg
    else:
        _log("Packing version %s" % version)
        eggpath, tmpdir = _build_egg(opts)
    if not _upload_egg(target, egg, project, version):
        exitcode = 1
    return exitcode, tmpdir


def _log(message):
    sys.stderr.write(message + os.linesep)


def _fail(message, code=1):
    _log(message)
    sys.exit(code)


def _get_targets():
    cfg = get_config()
    baset = dict(cfg.items("deploy")) if cfg.has_section("deploy") else {}
    targets = {}
    if "url" in baset:
        targets["default"] = baset
    for section in cfg.sections():
        if section.startswith("deploy:"):
            t = baset.copy()
            t.update(cfg.items(section))
            targets[section[7:]] = t
    return targets


def _get_target(name):
    try:
        return _get_targets()[name]
    except KeyError:
        raise _fail("Unknown target: %s" % name)


def _url(target, action):
    if "url" in target:
        return urljoin(target["url"], action)
    raise _fail("Error: Missing url for project")


def _get_version(target, opts):
    version = opts.version or target.get("version")
    if version == "HG":
        process = subprocess.Popen(
            ["hg", "tip", "--template", "{rev}"], stdout=subprocess.PIPE, universal_newlines=True
        )
        descriptor = "r%s" % process.communicate()[0]
        process = subprocess.Popen(["hg", "branch"], stdout=subprocess.PIPE, universal_newlines=True)
        name = process.communicate()[0].strip("\n")
        return "%s-%s" % (descriptor, name)
    elif version == "GIT":
        process = subprocess.Popen(["git", "describe"], stdout=subprocess.PIPE, universal_newlines=True)
        descriptor = process.communicate()[0].strip("\n")
        if process.wait() != 0:
            process = subprocess.Popen(
                ["git", "rev-list", "--count", "HEAD"],
                stdout=subprocess.PIPE,
                universal_newlines=True,
            )
            descriptor = "r%s" % process.communicate()[0].strip("\n")

        process = subprocess.Popen(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            stdout=subprocess.PIPE,
            universal_newlines=True,
        )
        name = process.communicate()[0].strip("\n")
        return "%s-%s" % (descriptor, name)
    elif version:
        return version
    else:
        return str(int(time.time()))


def _upload_egg(target, eggpath, project, version):
    with open(eggpath, "rb") as f:
        eggdata = f.read()
    data = {
        "project": project,
        "version": version,
        "egg": ("project.egg", eggdata),
    }
    body, content_type = encode_multipart_formdata(data)
    url = _url(target, "addversion.json")
    headers = {
        "Content-Type": content_type,
        "Content-Length": str(len(body)),
    }
    request = Request(url, body, headers)
    _add_auth_header(request, target)
    _log('Deploying to project "%s" in %s' % (project, url))
    return _http_post(request)


def _add_auth_header(request, target):
    url, username, password = (
        target["url"],
        target.get("username"),
        target.get("password", ""),
    )
    auth = get_auth(url=url, username=username, password=password)
    if auth:
        request.add_header(
            "Authorization", basic_auth_header(auth.username, auth.password)
        )


def _http_post(request):
    try:
        response = urlopen(request)
        _log("Server response (%s):" % response.code)
        print(response.read().decode())
        return True
    except HTTPError as e:
        _log("Deploy failed (%s):" % e.code)
        response = e.read().decode()
        try:
            data = json.loads(response)
        except ValueError:
            print(response)
        else:
            if "status" in data and "message" in data:
                print("Status: %(status)s" % data)
                print("Message:\n%(message)s" % data)
            else:
                print(json.dumps(data, indent=3))
    except URLError as e:
        _log("Deploy failed: %s" % e)


def _build_egg(opts):
    closest = closest_scrapy_cfg()
    os.chdir(os.path.dirname(closest))
    if not os.path.exists("setup.py"):
        settings = get_config().get("settings", "default")
        with open("setup.py", "w") as f:
            f.write(_SETUP_PY_TEMPLATE % {"settings": settings})
    tmpdir = tempfile.mkdtemp(prefix="scrapydeploy-")

    if opts.include_dependencies:
        _log("Including dependencies from requirements.txt")
        if not os.path.isfile("requirements.txt"):
            _fail("Error: Missing requirements.txt")
        command = "bdist_uberegg"
    else:
        command = "bdist_egg"

    kwargs = {} if opts.debug else {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    process = subprocess.run([sys.executable, "setup.py", "clean", "-a", command, "-d", tmpdir], check=True, **kwargs)

    eggpath = glob.glob(os.path.join(tmpdir, "*.egg"))[0]
    return eggpath, tmpdir


class HTTPRedirectHandler(UrllibHTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        newurl = newurl.replace(" ", "%20")
        if code in (301, 307):
            return Request(
                newurl,
                data=request.get_data(),
                headers=request.headers,
                origin_req_host=request.get_origin_req_host(),
                unverifiable=True,
            )
        elif code in (302, 303):
            newheaders = dict(
                (header, value)
                for header, value in request.headers.items()
                if header.lower() not in ("content-length", "content-type")
            )
            return Request(
                newurl,
                headers=newheaders,
                origin_req_host=request.get_origin_req_host(),
                unverifiable=True,
            )
        else:
            raise HTTPError(request.get_full_url(), code, msg, headers, fp)


if __name__ == "__main__":
    main()
