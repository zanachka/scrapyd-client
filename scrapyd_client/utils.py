import json
import netrc
import os
from configparser import BasicInterpolation, ConfigParser
from urllib.parse import urlparse

import requests
from requests.auth import HTTPBasicAuth
from scrapy.utils import conf

HEADERS = requests.utils.default_headers().copy()
HEADERS["User-Agent"] = "Scrapyd-client/1.2.3"
DEFAULT_TARGET_URL = "http://localhost:6800"


class EnvInterpolation(BasicInterpolation):
    """Interpolation which expands environment variables in values."""

    def before_get(self, parser, section, option, value, defaults):
        value = super().before_get(parser, section, option, value, defaults)
        return os.path.expandvars(value)


class ErrorResponse(Exception):  # noqa: N818
    """Raised when Scrapyd reports an error."""


class MalformedResponse(Exception):  # noqa: N818
    """Raised when the response can't be decoded."""


def _process_response(response):
    """Process the response object into a dictionary."""
    try:
        response = response.json()
    except json.decoder.JSONDecodeError as e:
        raise MalformedResponse(response.text) from e

    status = response["status"]
    if status == "ok":
        return response
    if status == "error":
        raise ErrorResponse(response["message"])
    raise RuntimeError(f"Unhandled response status: {status}")


def get_auth(url, username, password):
    """
    Retrieve authentication from arguments or infers from .netrc.

    :param url: The URL to check.
    :type url: str
    :param username: The username to use.
    :type username: str
    :param password: The password to use.
    :type password: str
    :returns: An HTTPBasicAuth object or None.
    :rtype: requests.auth.HTTPBasicAuth or None
    """
    if username:
        return HTTPBasicAuth(username=username, password=password)

    try:
        username, _account, password = netrc.netrc().authenticators(urlparse(url).hostname)
        return HTTPBasicAuth(username=username, password=password)
    except (OSError, netrc.NetrcParseError, TypeError):
        return None


def get_request(url, params=None, auth=None):
    """
    Dispatches a request with GET method.

    :param url: The URL to request.
    :type url: str
    :param params: The GET parameters.
    :type params: mapping
    :returns: The processed response.
    :rtype: mapping
    """
    if params is None:
        params = {}
    return _process_response(requests.get(url, params=params, headers=HEADERS, auth=auth))


def post_request(url, data, auth=None):
    """
    Dispatches a request with POST method.

    :param url: The URL to request.
    :type url: str
    :param data: The data to post.
    :returns: The processed response.
    :rtype: mapping
    """
    return _process_response(requests.post(url, data=data, headers=HEADERS, auth=auth))


def get_config(use_closest=True):
    """Get Scrapy config file as a ConfigParser."""
    cfg = ConfigParser(interpolation=EnvInterpolation())
    cfg.read(conf.get_sources(use_closest))
    return cfg
