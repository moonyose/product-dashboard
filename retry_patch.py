"""Make every requests.get/post retry transient network/SSL errors and 5xx."""
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

_s = requests.Session()
_r = Retry(total=4, connect=4, read=4, other=4, status=3, backoff_factor=2,
           status_forcelist=(500, 502, 503, 504), allowed_methods=("GET", "POST"),
           raise_on_status=False)
_s.mount("https://", HTTPAdapter(max_retries=_r))
_s.mount("http://", HTTPAdapter(max_retries=_r))

def _request(method, url, **kw):
    return _s.request(method, url, **kw)

requests.api.request = _request
for _m in ("get", "post", "put", "delete", "head"):
    setattr(requests, _m, (lambda m: lambda url, **kw: _request(m.upper(), url, **kw))(_m))
