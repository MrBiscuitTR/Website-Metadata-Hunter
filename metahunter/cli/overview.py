"""The overview printed when the program starts, so nobody has to read the docs first."""

from .. import __version__
from ..options import FILE_TYPES, INFO_CATEGORIES, SUBDOMAIN_SOURCES

OVERVIEW = """\
Website Metadata Hunter {version}
Collect a website's images, PDFs, documents, audio and video, and read their metadata
(GPS, timestamps, devices, authors, software, hidden text...). All parsing is offline.

USAGE
  python hunter.py                        this overview, then the guided wizard
  python hunter.py wizard                 answer a few questions, then scan
  python hunter.py scan <url> [options]   scan straight from the command line
  python hunter.py gui                    open the graphical interface
  python hunter.py inspect <path>...      read metadata of local files / folders (no network)
  python hunter.py pdf <file.pdf>...      redaction and hidden-text check of local PDFs (no network)
  python hunter.py subdomains <domain>    list subdomains from public sources
  python hunter.py help [command]         every option of a command

EXAMPLES
  python hunter.py scan example.com
      only that page, every file type
  python hunter.py scan https://example.com/press -d 2 --scope domain
      that page, the pages it links to and theirs, staying on example.com and its subdomains
  python hunter.py scan example.com -d 1 --types pdf,documents --info people,time,software
      documents only, and only who / when / with what
  python hunter.py scan example.com --subdomains links,crtsh --crawl-subdomains
      find subdomains (crawl hits + certificate logs) and crawl the live ones too
  python hunter.py scan example.com --wayback-files --types pdf
      also fetch PDFs the Wayback Machine archived (handy for deleted files)
  python hunter.py inspect ./downloads/report.docx ./photos
  python hunter.py pdf court_filing.pdf

MAIN SCAN OPTIONS ("python hunter.py help scan" lists all)
  -d, --depth N          link hops to follow from the start page; 0 = only that page    [0]
  --scope SCOPE          which links to follow: host, domain or any                      [host]
  --types LIST           {types}  [all]
  --info LIST            {info}
                         what goes into the report                                        [all]
  --subdomains LIST      {sources}
                         where to look for subdomains (third-party ones are opt-in)      [none]
  --no-redaction-check   skip the PDF hidden-text check (on by default)
  --max-files N          stop after N files                                               [500]

OUTPUT
  .scans/<site>_<ddMMyyyyHHmm>/  report.html, report.json, summary.txt, files/, pdf_analysis/
  Optional ExifTool: set exiftool_path in .config; the built-in parsers fill in what it misses.
"""


def overview_text():
    return OVERVIEW.format(version=__version__, types=",".join(FILE_TYPES), info=",".join(INFO_CATEGORIES),
                           sources=",".join(SUBDOMAIN_SOURCES))
