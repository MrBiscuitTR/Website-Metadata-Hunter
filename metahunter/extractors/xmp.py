"""XMP: Adobe RDF/XML metadata embedded in images, PDFs, audio, video and more.

The packet is read with a handful of regular expressions instead of an XML parser.
That keeps us immune to entity-expansion tricks and still copes with broken XML.
"""

import html
import re

MAX_PACKET_SIZE = 2 * 1024 * 1024
MAX_PACKETS = 8

NAME = r"[A-Za-z_][\w.-]*"
SIMPLE_ELEMENT = re.compile(rf"<({NAME}):({NAME})(?:\s[^<>]*)?>([^<]+)</\1:\2>")
ATTRIBUTE = re.compile(rf"\s({NAME}):({NAME})=\"([^\"]*)\"")
LIST_ELEMENT = re.compile(rf"<({NAME}):({NAME})(?:\s[^<>]*)?>\s*<rdf:(Seq|Bag|Alt)\b[^>]*>(.*?)</rdf:\3>", re.S)
LIST_ITEM = re.compile(r"<rdf:li\b[^>]*>([^<]*)</rdf:li>")

# Structural or very noisy namespaces that never hold anything a person cares about.
SKIPPED_PREFIXES = {"x", "rdf", "xmlns", "xml", "crs", "stDim", "stFnt", "xmpG", "xmpTPg", "stRef", "GPano"}

# "prefix:Name": (label, category)
KNOWN_FIELDS = {
    "dc:creator": ("Creator", "people"),
    "dc:rights": ("Rights", "people"),
    "dc:publisher": ("Publisher", "people"),
    "dc:contributor": ("Contributor", "people"),
    "dc:title": ("Title", "document"),
    "dc:description": ("Description", "document"),
    "dc:subject": ("Keywords", "document"),
    "dc:language": ("Language", "document"),
    "dc:format": ("MIME type", "technical"),
    "xmp:CreateDate": ("Created", "time"),
    "xmp:ModifyDate": ("Modified", "time"),
    "xmp:MetadataDate": ("Metadata changed", "time"),
    "xmp:CreatorTool": ("Creator tool", "software"),
    "xmp:Rating": ("Rating", "document"),
    "xmp:Label": ("Label", "document"),
    "pdf:Producer": ("PDF producer", "software"),
    "pdf:Keywords": ("Keywords", "document"),
    "pdf:Author": ("Author", "people"),
    "photoshop:DateCreated": ("Date created", "time"),
    "photoshop:City": ("City", "location"),
    "photoshop:State": ("State / province", "location"),
    "photoshop:Country": ("Country", "location"),
    "photoshop:AuthorsPosition": ("Author position", "people"),
    "photoshop:Credit": ("Credit", "people"),
    "photoshop:Source": ("Source", "people"),
    "photoshop:CaptionWriter": ("Caption writer", "people"),
    "photoshop:Headline": ("Headline", "document"),
    "photoshop:Instructions": ("Instructions", "document"),
    "Iptc4xmpCore:Location": ("Sublocation", "location"),
    "Iptc4xmpCore:CountryCode": ("Country code", "location"),
    "Iptc4xmpCore:CiEmailWork": ("Creator email", "people"),
    "Iptc4xmpCore:CiTelWork": ("Creator phone", "people"),
    "Iptc4xmpCore:CiUrlWork": ("Creator website", "people"),
    "Iptc4xmpCore:CiAdrExtadr": ("Creator address", "location"),
    "Iptc4xmpCore:CiAdrCity": ("Creator city", "location"),
    "Iptc4xmpCore:CiAdrCtry": ("Creator country", "location"),
    "Iptc4xmpExt:City": ("City", "location"),
    "Iptc4xmpExt:CountryName": ("Country", "location"),
    "Iptc4xmpExt:ProvinceState": ("State / province", "location"),
    "Iptc4xmpExt:Sublocation": ("Sublocation", "location"),
    "exif:DateTimeOriginal": ("Taken", "time"),
    "exif:GPSAltitude": ("GPS altitude", "location"),
    "exif:GPSLatitude": ("GPS latitude (raw)", "location"),
    "exif:GPSLongitude": ("GPS longitude (raw)", "location"),
    "tiff:Make": ("Camera make", "device"),
    "tiff:Model": ("Camera model", "device"),
    "tiff:Software": ("Software", "software"),
    "tiff:Artist": ("Artist", "people"),
    "aux:SerialNumber": ("Camera serial number", "device"),
    "aux:Lens": ("Lens", "device"),
    "aux:LensID": ("Lens ID", "device"),
    "aux:LensSerialNumber": ("Lens serial number", "device"),
    "aux:OwnerName": ("Camera owner", "people"),
    "exifEX:LensMake": ("Lens make", "device"),
    "exifEX:LensModel": ("Lens model", "device"),
    "exifEX:BodySerialNumber": ("Camera serial number", "device"),
    "exifEX:LensSerialNumber": ("Lens serial number", "device"),
    "exifEX:CameraOwnerName": ("Camera owner", "people"),
    "xmpMM:DocumentID": ("Document ID", "hidden"),
    "xmpMM:OriginalDocumentID": ("Original document ID", "hidden"),
    "xmpMM:InstanceID": ("Instance ID", "hidden"),
    "stEvt:softwareAgent": ("Edit history: software", "software"),
    "stEvt:when": ("Edit history: time", "time"),
    "stEvt:action": ("Edit history: action", "document"),
    "xmpRights:Owner": ("Rights owner", "people"),
    "xmpRights:UsageTerms": ("Usage terms", "document"),
    "xmpRights:WebStatement": ("Rights statement URL", "people"),
    "lr:hierarchicalSubject": ("Keywords (Lightroom)", "document"),
    "xmpDM:artist": ("Artist", "people"),
    "xmpDM:album": ("Album", "document"),
    "xmpDM:composer": ("Composer", "people"),
    "xmpDM:engineer": ("Engineer", "people"),
}

# Where unknown fields of a familiar namespace land.
PREFIX_CATEGORIES = {"exif": "camera", "tiff": "technical", "aux": "device", "exifEX": "device",
                     "photoshop": "document", "xmp": "document", "dc": "document", "xmpDM": "technical"}


def find_packets(data):
    """Locate raw XMP packets anywhere inside a file."""
    packets = []
    for opening, closing in ((b"<x:xmpmeta", b"</x:xmpmeta>"), (b"<rdf:RDF", b"</rdf:RDF>")):
        start = 0
        while len(packets) < MAX_PACKETS:
            begin = data.find(opening, start)
            if begin < 0:
                break
            end = data.find(closing, begin)
            if end < 0:
                break
            packets.append(data[begin:end + len(closing)])
            start = end
        if packets:
            break
    return packets


def parse_coordinate(text):
    """XMP GPS values look like 51,30.123N or 51,30,7.4N."""
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?),(\d+(?:\.\d+)?)(?:,(\d+(?:\.\d+)?))?([NSEW])\s*", text)
    if not match:
        return None
    degrees = float(match.group(1)) + float(match.group(2)) / 60 + float(match.group(3) or 0) / 3600
    return -degrees if match.group(4) in "SW" else degrees


def _report(meta, prefix, name, value):
    if prefix in SKIPPED_PREFIXES:
        return
    value = html.unescape(value).strip()
    if not value:
        return
    key = f"{prefix}:{name}"
    if key in KNOWN_FIELDS:
        label, category = KNOWN_FIELDS[key]
        meta.add(category, f"XMP {label}", value)
    else:
        meta.add(PREFIX_CATEGORIES.get(prefix, "hidden"), f"XMP {key}", value)


def parse_xmp(packet, meta):
    """Read one XMP packet (bytes or str) into `meta`."""
    if isinstance(packet, (bytes, bytearray)):
        packet = bytes(packet[:MAX_PACKET_SIZE]).decode("utf-8", "replace")
    text = packet[:MAX_PACKET_SIZE]

    gps = {}
    for prefix, name, _list_kind, items in LIST_ELEMENT.findall(text):
        values = [html.unescape(item).strip() for item in LIST_ITEM.findall(items)]
        _report(meta, prefix, name, "; ".join(value for value in values if value))
    for prefix, name, value in SIMPLE_ELEMENT.findall(text):
        if name in ("GPSLatitude", "GPSLongitude"):
            gps[name] = value
        _report(meta, prefix, name, value)
    for prefix, name, value in ATTRIBUTE.findall(text):
        if name in ("GPSLatitude", "GPSLongitude"):
            gps[name] = value
        _report(meta, prefix, name, value)

    if "GPSLatitude" in gps and "GPSLongitude" in gps:
        latitude, longitude = parse_coordinate(gps["GPSLatitude"]), parse_coordinate(gps["GPSLongitude"])
        if latitude is not None and longitude is not None:
            meta.add_gps(latitude, longitude, "XMP GPS")


def parse_all_packets(data, meta):
    """Find and read every XMP packet inside `data`. Returns how many were found."""
    packets = find_packets(data)
    for packet in packets:
        parse_xmp(packet, meta)
    return len(packets)
