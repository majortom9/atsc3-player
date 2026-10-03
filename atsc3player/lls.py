"""ATSC 3.0 Low Level Signalling (A/331 section 6): the SLT and friends.

LLS arrives on 224.0.23.60:4937. Each UDP payload is a 4-byte header
(LLS_table_id, LLS_group_id, group_count_minus1, LLS_table_version) and a
gzip-compressed XML table. Stations that sign their LLS send table 0xFE
(SignedMultiTable) instead: a count, then per table its id, version, 16-bit
length and gzip payload, followed by the signature.
"""

import gzip
import struct
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

LLS_ADDRESS = ("224.0.23.60", 4937)

TABLE_SLT = 1
TABLE_RRT = 2
TABLE_SYSTEM_TIME = 3
TABLE_AEAT = 4
TABLE_ONSCREEN_MESSAGE = 5
TABLE_CDT = 6
TABLE_SIGNED_MULTI = 0xFE
TABLE_USER_DEFINED = 0xFF

SERVICE_CATEGORIES = {
    1: "linear A/V", 2: "linear audio", 3: "app-based", 4: "ESG",
    5: "EAS", 6: "DRM data",
}
SLS_PROTOCOLS = {1: "ROUTE", 2: "MMTP"}


@dataclass
class Service:
    service_id: int
    major: int
    minor: int
    name: str
    category: int
    protected: bool = False
    hidden: bool = False
    sls_protocol: int = 0
    sls_dst: str = ""
    sls_port: int = 0
    sls_src: str = ""
    global_id: str = ""
    extra: dict = field(default_factory=dict)

    @property
    def channel(self):
        return f"{self.major}.{self.minor}" if self.major else "-"

    @property
    def category_name(self):
        return SERVICE_CATEGORIES.get(self.category, f"category {self.category}")

    @property
    def playable(self):
        """Something this player can show: unprotected linear A/V over ROUTE."""
        return (self.sls_protocol == 1 and self.category == 1 and not self.protected
                and bool(self.sls_dst) and bool(self.sls_port))


def _local(tag):
    return tag.rsplit("}", 1)[-1]


def split_tables(payload):
    """One LLS UDP payload -> [(table_id, version, xml_bytes)]; [] if unusable."""
    if len(payload) < 5:
        return []
    table_id, _group, _count, version = payload[:4]
    body = payload[4:]
    if table_id == TABLE_SIGNED_MULTI:
        out = []
        n, i = body[0], 1
        for _ in range(n):
            if i + 4 > len(body):
                break
            tid, ver = body[i], body[i + 1]
            ln = struct.unpack("!H", body[i + 2:i + 4])[0]
            i += 4
            try:
                out.append((tid, ver, gzip.decompress(body[i:i + ln])))
            except (OSError, EOFError):
                pass
            i += ln
        return out
    try:
        return [(table_id, version, gzip.decompress(body))]
    except (OSError, EOFError):
        return []


def parse_slt(xml_bytes):
    """SLT XML -> (bsid, [Service])."""
    root = ET.fromstring(xml_bytes)
    services = []
    for svc in root:
        if _local(svc.tag) != "Service":
            continue
        a = svc.attrib
        s = Service(
            service_id=int(a.get("serviceId", 0)),
            major=int(a.get("majorChannelNo", 0) or 0),
            minor=int(a.get("minorChannelNo", 0) or 0),
            name=a.get("shortServiceName", ""),
            category=int(a.get("serviceCategory", 0) or 0),
            protected=a.get("protected", "false").lower() == "true",
            hidden=a.get("hidden", "false").lower() == "true",
            global_id=a.get("globalServiceID", ""),
        )
        for child in svc:
            if _local(child.tag) == "BroadcastSvcSignaling":
                c = child.attrib
                s.sls_protocol = int(c.get("slsProtocol", 0) or 0)
                s.sls_dst = c.get("slsDestinationIpAddress", "")
                s.sls_port = int(c.get("slsDestinationUdpPort", 0) or 0)
                s.sls_src = c.get("slsSourceIpAddress", "")
        services.append(s)
    return root.attrib.get("bsid"), services


class LlsMonitor:
    """Feed LLS UDP payloads; keeps the latest SLT (version-checked)."""

    def __init__(self, on_slt=None):
        self.on_slt = on_slt
        self.slt_version = None
        self.bsid = None
        self.services = []
        self.system_time = None

    def feed(self, payload):
        for tid, ver, xml in split_tables(payload):
            if tid == TABLE_SLT and ver != self.slt_version:
                try:
                    self.bsid, self.services = parse_slt(xml)
                except ET.ParseError:
                    continue
                self.slt_version = ver
                if self.on_slt:
                    self.on_slt(self.services)
            elif tid == TABLE_SYSTEM_TIME:
                self.system_time = xml
