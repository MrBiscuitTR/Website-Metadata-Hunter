"""Archived copies of a site's files from the Wayback Machine.

Useful for files that were taken down: the archive often still has the original,
metadata and all. The query goes to web.archive.org; the files come from there too.
"""

import json
from urllib.parse import quote


from ..net.http import FetchError
from ..net.urls import extension_of
from ..options import category_for_extension, category_for_mime

MAX_RESULTS = 2000
MIME_FILTERS = {"images": r"image/.*", "pdf": r"application/pdf", "documents": r"application/(msword|vnd\..*)",
                "audio": r"audio/.*", "video": r"video/.*"}



def archived_files(domain, include_subdomains, file_types, fetcher, events, limit=MAX_RESULTS):
    """[(archive download URL, original URL, category)] for files of the wanted types."""
    mime_pattern = "|".join(MIME_FILTERS[name] for name in file_types if name in MIME_FILTERS)
    match_type = "domain" if include_subdomains else "host"
    url = (f"https://web.archive.org/cdx/search/cdx?url={domain}&matchType={match_type}&output=json"
           f"&fl=timestamp,original,mimetype&collapse=urlkey&filter=statuscode:200"
           f"&filter={quote('mimetype:(' + mime_pattern + ')')}&limit={limit}")

    events.log("Asking the Wayback Machine for archived files...")
    try:
        rows = json.loads(fetcher.get(url, 64 * 1024 * 1024, timeout=90).body or b"[]")
    except (FetchError, ValueError) as error:
        events.log(f"  Wayback Machine query failed: {error}")
        return []
    results = []
    for row in rows[1:]:      # first row is the header
        if len(row) < 3:
            continue
        timestamp, original, mime = row[0], row[1], row[2]
        category = category_for_extension(extension_of(original), file_types) or category_for_mime(mime, file_types)
        if category:
            # "id_" asks for the original bytes rather than the archive's rewritten page.
            results.append((f"https://web.archive.org/web/{timestamp}id_/{original}", original, category))
    events.log(f"  {len(results)} archived files found")
    return results
