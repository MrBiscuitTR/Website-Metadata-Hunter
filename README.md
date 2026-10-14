# Website Metadata Hunter

Collects a website's images, PDFs, office documents, audio and video, and reads what is
hidden inside them: GPS positions, capture and edit times, camera and phone models, serial
numbers, authors, reviewers, software, leaked folder paths, and text that someone tried
to black out in a PDF.

- **Standard library only.** Nothing to `pip install`. Python 3.9 or newer.
- **Offline processing.** The network is only used to download the site's files (and, if you
  opt in, to ask public subdomain sources). Every file is parsed locally.
- **Terminal and GUI.** Same options, same results.

## Quick start

```
git clone https://github.com/<you>/Website-Metadata-Hunter.git
cd Website-Metadata-Hunter
```

Then start whichever interface you prefer:

```
python hunter.py          # terminal: prints every command with examples, then offers a guided wizard
python hunter.py gui      # graphical interface
```

On macOS / Linux use `python3` if `python` points to Python 2.

| System | Python | GUI (tkinter) |
| --- | --- | --- |
| Windows | python.org installer | included |
| macOS | python.org installer or Homebrew `python-tk` | included / `brew install python-tk` |
| Debian / Ubuntu | `sudo apt install python3` | `sudo apt install python3-tk` |

`requirements.txt` is intentionally empty.

## Commands

```
python hunter.py                        overview, then the guided wizard
python hunter.py wizard                 answer a few questions, then scan
python hunter.py scan <url> [options]   scan straight from the command line
python hunter.py gui                    graphical interface
python hunter.py inspect <path>...      metadata of local files / folders (no network)
python hunter.py pdf <file.pdf>...      redaction and hidden-text check of local PDFs (no network)
python hunter.py subdomains <domain>    list subdomains from public sources
python hunter.py help [command]         every option of a command
```

Examples:

```
python hunter.py scan example.com                                   # only that page
python hunter.py scan https://example.com/press -d 2 --scope domain # two link hops, *.example.com
python hunter.py scan example.com -d 1 --types pdf,documents --info people,time,software
python hunter.py scan example.com --subdomains links,crtsh --crawl-subdomains
python hunter.py scan example.com --wayback-files --types pdf      # archived copies of deleted files
python hunter.py scan example.com --view category,host             # print results grouped two ways
python hunter.py inspect ./downloads ./photos/IMG_0042.HEIC
python hunter.py pdf court_filing.pdf
```

### Choosing how far to go

| Option | Meaning |
| --- | --- |
| `-d / --depth N` | link hops from the start page. `0` reads only that page (default). |
| `--scope host\|domain\|any` | which links may be followed: same host, the host and its subdomains, or anywhere. |
| `--no-external-files` | by default files that in-scope pages embed from CDNs are collected too; this turns it off. |
| `--sitemap` | also visit pages listed in `sitemap.xml` / `robots.txt` sitemaps. |
| `--subdomains LIST` | `links` (hosts seen while crawling) plus opt-in third-party sources: `crtsh`, `certspotter`, `hackertarget`, `alienvault`, `wayback`. |
| `--crawl-subdomains` | crawl the subdomains that resolve instead of only listing them. |
| `--wayback-files` | also download archived copies of files from the Wayback Machine. |
| `--types LIST` | `images`, `pdf`, `documents`, `audio`, `video` (default all). |
| `--info LIST` | what goes into reports: `location`, `time`, `device`, `camera`, `people`, `software`, `document`, `technical`, `hidden`, `http`. |
| `--view LIST` | what to print when done: `summary`, `category`, `file`, `page`, `host`, `type`. |
| `--no-report-files` | print results only; write no report files. |

`python hunter.py help scan` lists the rest (limits, threads, delay, timeouts, user agent, TLS).

### Pause, stop, restart

- **Terminal:** press **Ctrl+C** at any time. The scan pauses at once and asks:
  continue, stop (report is written with what was collected), restart, or quit now.
  A second Ctrl+C quits immediately.
- **GUI:** Pause / Continue, Stop, Restart buttons. Closing the window during a scan asks first.

## What you get

Each scan gets its own folder, named after the target plus a `ddMMyyyyHHmm` timestamp:

```
.scans/blog-example-com_press_141020261530/
  report.html        open in a browser: highlights, GPS links, every file's metadata
  report.json        everything, machine readable
  summary.txt        the highlights as plain text
  scan.log           what happened, with times
  subdomains.txt     if any were found
  files/<host>/      copies of the downloaded files (turn off with --no-save-files)
  embedded/          thumbnails, cover art, attachments and appended data found inside files
  pdf_analysis/<pdf>/  redaction check results, see below
```

The **highlights** answer "what did this site leak?": every distinct person, company,
email, device, serial number, software and place, and the files each came from.

## The PDF redaction check

On by default for every PDF (`--no-redaction-check` turns it off). It interprets the page
content to find text that is in the file but not meant to be read:

- text painted over by a later black (or any opaque) box or image, the classic failed redaction;
- text under Redact annotations that were never applied, or under filled / dark highlight annotations;
- white text, text placed outside the page, microscopic text; invisible text is listed too
  (normal for OCR layers on scans, so it does not raise the alarm on its own);
- text that only exists in **earlier saved revisions** of the file (incremental saves keep the old content);
- content streams that no page draws any more.

Transparency and clipping are taken into account, so see-through or clipped shapes do not
produce false alarms. For each PDF, `pdf_analysis/<name>/` holds a plain-language
`summary.txt`, `findings.json`, all text rebuilt per page, every text run and painted shape
with position, colour and paint order (TSV, easy to diff), an object listing, each earlier
revision as its own PDF, and any attached files.

## Supported formats (built-in parsers)

| Kind | Formats | Highlights |
| --- | --- | --- |
| Images | JPEG, PNG/APNG, GIF, WebP, TIFF and TIFF-based raw, BMP, ICO/CUR, SVG, HEIC/HEIF, AVIF | EXIF incl. GPS, serials, lens, owner; XMP incl. edit history; IPTC; ICC; PNG text chunks incl. AI prompts; C2PA presence; EXIF thumbnails; data appended after the image |
| PDF | all versions, object streams, incremental updates | Info dictionary of every saved version, XMP, annotation authors, form values, JavaScript and launch actions, attachments, EXIF of embedded photos, fonts |
| Documents | DOCX/XLSX/PPTX (+ macro variants), ODT/ODS/ODP, DOC/XLS/PPT | authors, last editor, company, template, edit time, tracked-change and comment authors, saved-from folders, hidden sheets, printer names, external links, macros, metadata of embedded pictures |
| Audio | MP3 (ID3v1/v2), WAV/BWF, FLAC, Ogg Vorbis, Opus | tags, encoder, recorder (bext), cover art |
| Video | MP4, MOV, M4A, 3GP, AVI, MKV, WebM | creation time, phone make/model/software, GPS (ISO 6709), titles, muxing apps |

Every file is also searched for Windows / macOS / Linux user folders, network shares and
email addresses (`--no-leak-scan` turns it off). Files that turn out to be identical
(same SHA-256) are only read once.

## Optional: ExifTool

ExifTool knows thousands more tags. To use it, set its path in `.config`:

```
exiftool_path = C:\Tools\exiftool.exe
```

Quoted or unquoted, `/` or `\`, doubled backslashes, relative to the app folder, a folder
that contains the executable (also the unzipped Windows package), `~`, `$VAR` / `%VAR%`,
`file://` URIs and Git Bash / WSL style paths all work. When it points to a working
ExifTool **12.24 or newer**, ExifTool reads the regular tags and the built-in parsers still
add what it does not report (reviewer names inside documents, PDF revisions and forensics,
appended data, leaked paths, embedded thumbnails). If the setting is empty or invalid,
only the built-in parsers are used. Files are piped to ExifTool through stdin, so no
temporary copies are written; older ExifTool versions are refused because they can run
code from crafted image files (CVE-2021-22204).

## Safety and privacy

- **Nothing is deleted, ever.** The program has no code that removes files or folders.
- **Nothing is overwritten.** All writes go through `metahunter/output.py`: files are created
  in exclusive mode, confined to the scan folder (names from websites cannot escape it), and a
  name clash gets a numbered suffix. Folders are only created when something is saved.
- **Untrusted input is parsed defensively.** No `eval`, no pickle, no XML parser (so no entity
  expansion attacks), hard limits on decompressed sizes, nesting depth, counts and file sizes.
  A malformed file becomes a warning, never a crash.
- **Network guard.** Only `http` / `https`, also after redirects. Hosts that resolve to private,
  loopback or link-local addresses are refused unless you pass `--allow-private`, so a
  public page cannot make the scanner probe your local network. `robots.txt` is obeyed by
  default and requests are spaced out (`--delay`).
- **Third parties are opt-in.** Only the subdomain sources and the Wayback Machine send your
  target's domain to someone else, and only when you choose them.
- **The HTML report cannot run anything.** No scripts, no external resources, enforced by a
  Content-Security-Policy; every value is escaped.

Only scan sites you are allowed to scan.

## For auditors: where things live

```
hunter.py                  launcher
metahunter/
  options.py               every scan setting (ScanOptions) and the category lists
  runner.py                the scan, phase by phase: subdomains, crawl, archive, download, read, report
  control.py               pause / stop / abort and the worker pool
  output.py                the only code that writes to disk
  config.py                reads .config
  net/        urls.py (scope rules), http.py (the one HTTP client)
  discovery/  html_links.py, crawler.py, subdomains.py, wayback.py
  extractors/ one module per format family, metadata.py (the result container),
              leaks.py (path / email search), exiftool.py (optional ExifTool bridge)
  pdf/        objects.py (syntax), document.py (objects, revisions), filters.py (stream decoding),
              fonts.py (text decoding), content.py (page interpreter), structure.py,
              redaction.py (the checker), report.py (its output files)
  report/     highlights.py, writers.py (JSON / text), html_report.py
  cli/        overview.py, arguments.py, main.py, wizard.py, views.py, terminal.py
  gui/        app.py
tests/                     python -m unittest discover -s tests -t .
```

## Known limits

- Pages are read as delivered by the server; content that JavaScript builds in the browser is not seen.
- Encrypted PDFs: structure and dates are read, but encrypted strings and streams are not decrypted.
- Files larger than `--max-size` (50 MB by default) are skipped.
