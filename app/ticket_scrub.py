"""Replaces secrets and identifying details in a ticket report with placeholders.

Two layers: (a) exact values the bridge knows are secret (tokens, provider hosts, account names...) in plain,
URL-encoded and JSON-escaped form; (b) patterns for things nobody told us about (IPs, JWTs, key=value pairs,
URL credentials, emails, hosts, long token-looking runs). Numbered placeholders are stable per distinct
value (<IP-1>, <IP-2>) so a reader can still tell two things apart. Pure string work, no I/O.
"""
import json
import re
from urllib.parse import quote

# kind -> (placeholder label, numbered?)
KINDS = {
    "token": ("TOKEN", False), "api_key": ("API-KEY", False), "password": ("PASSWORD", False), "email": ("EMAIL", False),
    "ip": ("IP", True), "host": ("HOST", True), "provider": ("PROVIDER", True), "account": ("ACCOUNT", True),
}
MIN_SECRET_LEN = 4
KEEP_IPS = {"127.0.0.1", "0.0.0.0", "255.255.255.255"}
KEEP_HOSTS = {"github.com", "ghcr.io", "themoviedb.org", "api.themoviedb.org", "image.tmdb.org", "plex.tv", "docker.io"}
TLDS = "com|net|org|tv|io|me|co|info|xyz|cc|us|uk|to|stream|live|app|dev|site|online|top|vip|club|pro|fun|pw|ws"

_URL_CREDS = re.compile(r"(?P<scheme>[a-zA-Z][a-zA-Z0-9+.-]*://)(?P<user>[^\s/:@]+):(?P<pw>[^\s/@]+)@")
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")
_KV = re.compile(r"(?i)\b(?P<k>x-plex-token|api[_-]?key|apikey|access[_-]?token|token|passwd|password|secret)(?P<sep>=)(?P<v>[^&\s\"']+)")
_JSON_KV = re.compile(r"(?i)(?P<pre>\"(?:x-plex-token|api[_-]?key|apikey|access[_-]?token|token|passwd|password|secret)\"\s*:\s*\")(?P<v>[^\"]*)(?=\")")
_USER_KV = re.compile(r"(?i)\b(?P<k>username|user)(?P<sep>=)(?P<v>[^&\s\"']+)")
_EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_IPV4 = re.compile(r"(?<![\d.])(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)(?![\d.])")
_IPV6 = re.compile(r"(?<![\w:])(?:[0-9A-Fa-f]{1,4}:){7}[0-9A-Fa-f]{1,4}(?![\w:])|(?<![\w:])(?:[0-9A-Fa-f]{1,4}:){1,6}:(?:[0-9A-Fa-f]{1,4}(?::[0-9A-Fa-f]{1,4}){0,5})?(?![\w:])")
_HEXRUN = re.compile(r"\b[0-9a-fA-F]{32,}\b")
_LONGTOKEN = re.compile(r"\b(?=[A-Za-z0-9_-]*\d)(?=[A-Za-z0-9_-]*[A-Za-z])[A-Za-z0-9_-]{40,}\b")
_HOST = re.compile(r"(?i)\b(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+(?:%s)\b(?::\d+)?" % TLDS)


class Scrubber:
    def __init__(self, secrets: dict[str, list[str]] | None = None):
        self._maps: dict[str, dict[str, int]] = {}
        self._counts: dict[str, int] = {}
        pairs = []
        for kind, values in (secrets or {}).items():
            if kind not in KINDS:
                continue
            for v in values:
                v = (v or "").strip()
                if len(v) < MIN_SECRET_LEN:
                    continue
                for form in {v, quote(v, safe=""), json.dumps(v)[1:-1]}:
                    pairs.append((form, kind, v))
        # longest first so a host is replaced before a shorter word inside it
        self._exact = sorted(pairs, key=lambda p: -len(p[0]))

    def _ph(self, kind: str, value: str) -> str:
        label, numbered = KINDS[kind]
        self._counts[kind] = self._counts.get(kind, 0) + 1
        if not numbered:
            return f"<{label}>"
        m = self._maps.setdefault(kind, {})
        n = m.setdefault(value.lower(), len(m) + 1)
        return f"<{label}-{n}>"

    def counts(self) -> dict[str, int]:
        return dict(self._counts)

    def scrub(self, text: str) -> str:
        for form, kind, canon in self._exact:
            if form in text:
                # one placeholder per canonical value, however it was written
                text = text.replace(form, self._ph_once(kind, canon, text, form))
        text = _URL_CREDS.sub(lambda m: f"{m['scheme']}{self._ph('account', m['user'])}:{self._ph('password', m['pw'])}@", text)
        text = _JWT.sub(lambda m: self._ph("token", m[0]), text)
        text = _JSON_KV.sub(lambda m: m["pre"] + self._unless_ph(m["v"], "token"), text)
        text = _KV.sub(lambda m: f"{m['k']}{m['sep']}{self._unless_ph(m['v'], 'password' if m['k'].lower() in ('password', 'passwd') else ('api_key' if 'key' in m['k'].lower() else 'token'))}", text)
        text = _USER_KV.sub(lambda m: f"{m['k']}{m['sep']}{self._unless_ph(m['v'], 'account')}", text)
        text = _EMAIL.sub(lambda m: self._ph("email", m[0]), text)
        text = _IPV4.sub(lambda m: m[0] if m[0] in KEEP_IPS else self._ph("ip", m[0]), text)
        text = _IPV6.sub(lambda m: self._ph("ip", m[0]), text)
        text = _HEXRUN.sub(lambda m: self._ph("token", m[0]), text)
        text = _LONGTOKEN.sub(lambda m: self._ph("token", m[0]), text)
        text = _HOST.sub(lambda m: m[0] if m[0].split(":")[0].lower() in KEEP_HOSTS else self._ph("host", m[0].split(":")[0]), text)
        return text

    def _ph_once(self, kind: str, canon: str, text: str, form: str) -> str:
        # count each occurrence of the form, but number by the canonical value
        n = text.count(form)
        out = self._ph(kind, canon)
        self._counts[kind] += n - 1
        return out

    def _unless_ph(self, value: str, kind: str) -> str:
        return value if value.startswith("<") and value.endswith(">") else self._ph(kind, value)
