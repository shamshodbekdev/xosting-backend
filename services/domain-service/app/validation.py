"""Domain-name and DNS-record validation (RFC 1035 / 3696 / 6844 rules)."""

import ipaddress
import re

_LABEL = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)$")
_HOSTNAME = re.compile(r"^(\*\.)?((?!-)[a-z0-9_-]{1,63}(?<!-)\.)*(?!-)[a-z0-9_-]{1,63}(?<!-)\.?$")


class InvalidDomain(ValueError):
    pass


def normalise_domain(raw: str) -> str:
    """Lower-case, strip, IDNA-encode and syntax-check a registrable domain."""
    value = raw.strip().lower().rstrip(".").removeprefix("www.")
    try:
        value = value.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise InvalidDomain("Domain contains invalid characters.") from exc
    labels = value.split(".")
    if len(value) > 253 or len(labels) < 2 or not all(_LABEL.match(label) for label in labels):
        raise InvalidDomain("Invalid domain name.")
    if labels[-1].isdigit():
        raise InvalidDomain("Top-level domain cannot be numeric.")
    return value


def tld_of(domain: str) -> str:
    return domain.rsplit(".", 1)[1]


def is_hostname(value: str) -> bool:
    return len(value) <= 253 and bool(_HOSTNAME.match(value.lower()))


def validate_record(record_type: str, name: str, content: str, priority: int | None) -> str:
    """Return normalised content or raise ``ValueError`` with a readable message."""
    if name != "@" and not is_hostname(name):
        raise ValueError("Record name must be '@' or a valid relative hostname.")

    content = content.strip()
    match record_type:
        case "A":
            ipaddress.IPv4Address(content)
        case "AAAA":
            ipaddress.IPv6Address(content)
        case "CNAME" | "NS":
            if not is_hostname(content):
                raise ValueError(f"{record_type} content must be a hostname.")
            if record_type == "CNAME" and name == "@":
                raise ValueError("CNAME is not allowed at the zone apex.")
        case "MX":
            if priority is None:
                raise ValueError("MX records require a priority.")
            if not is_hostname(content):
                raise ValueError("MX content must be a hostname.")
        case "SRV":
            if priority is None:
                raise ValueError("SRV records require a priority.")
            parts = content.split()
            if len(parts) != 3 or not parts[0].isdigit() or not parts[1].isdigit():
                raise ValueError("SRV content must be '<weight> <port> <target>'.")
        case "CAA":
            if not re.match(r'^\d{1,3} (issue|issuewild|iodef) "[^"]*"$', content):
                raise ValueError('CAA content must look like: 0 issue "letsencrypt.org"')
        case "TXT":
            if len(content) > 4096:
                raise ValueError("TXT content is too long.")
    return content
