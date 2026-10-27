# Chikari Scraper

A Python script that downloads chapter text from Chikari.moe, saves each
chapter as an individual HTML file, and builds an EPUB from the saved
chapters.

The script uses Playwright because the chapter content is rendered by
JavaScript. It looks for the chapter body in `div.novel-text` to avoid
accidentally saving comments or unrelated page content.

> Use this script only for material you have permission to archive.
> Respect the website's terms and avoid excessive requests.

## Requirements

-   Python 3.9 or newer recommended
-   An internet connection
-   Permission to access and archive the source material

## 1. Install dependencies

Open a terminal in the folder containing `chikari_to_epub.py` and run:

``` bash
python -m pip install playwright beautifulsoup4 ebooklib
python -m playwright install chromium
```

On some systems, use `python3` instead of `python`.

## 2. Start a download

``` bash
python chikari_to_epub.py \
  --url "https://chikari.moe/novels/beyond-the-timescape/1" \
  --title "Beyond the Timescape" \
  --author "Er Gen" \
  --output "Beyond the Timescape.epub"
```
Where the values are changed accordingly. Make sure the URL is of the first chapter and not the for the page itself. The values above are default. 
``` bash
python chikari_to_epub.py
```
Will download Beyond the Timescape by Er Gen.

By default, the script attempts up to 2,000 chapters in one run,
stopping earlier if it cannot find a next chapter. This can be changed with the --max-chapters command line option.
It saves chapter HTML files in `[title]/` where [title] is the value passed to --title (make sure this does not conflict with any preexisting folders), records downloaded chapters in
`[title]/manifest.json`, stores the resume point in
`[title]/progress.json`, and creates the EPUB from
the chapters saved so far. 
There is a delay between chapter downloads of 1.2 seconds to prevent rate limiting errors, which can be changed with the --delay option.

All command line options can be viewed by running
``` bash
python chikari_to_epub.py -h
```

Note that no cover picture will be downloaded. One can be added easily through EPUB editing software such as Calibre.

## Resume after an interruption

``` bash
python chikari_to_epub.py --resume\
  --url "https://chikari.moe/novels/beyond-the-timescape/1" \
  --title "Beyond the Timescape" \
  --author "Er Gen" \
  --output "Beyond the Timescape.epub"
```

Resume uses the pending URL and chapter number stored in
`chapters/progress.json`. Keep the entire `chapters` folder, including
both JSON files. Use the same values for any command line options.

If the script cannot find a valid `div.novel-text` body, it stops rather
than risk saving comments as chapter text. After checking the page or
fixing the cause, run the resume command again.

## Start at a specific chapter

For example, start at chapter 150:

``` bash
python chikari_to_epub.py --start 150
```

This constructs a numbered chapter URL based on the default series URL.
It is a manual starting point, not the same as `--resume`.

## Rebuild the EPUB without downloading

``` bash
python chikari_to_epub.py --build-epub-only
```

This uses the saved chapter files and manifest. It does not fetch
chapters from the website.

## Files created

``` text
your-folder/
├── chikari_to_epub.py
├── Beyond the Timescape.epub
└── Beyond the Timescape/
    ├── chapter_00001.html
    ├── chapter_00002.html
    ├── manifest.json
    └── progress.json
```

-   `chapter_XXXXX.html`: saved chapter content, one file per chapter.
-   `manifest.json`: chapter numbers, titles, source URLs, and
    filenames.
-   `progress.json`: the next URL and chapter number used by `--resume`.
-   `Beyond the Timescape.epub`: EPUB built from the saved chapters.

The script saves a chapter before advancing its manifest and progress
state to reduce the risk of losing completed work if interrupted.

## Troubleshooting

### `No module named ...`

``` bash
python -m pip install playwright beautifulsoup4 ebooklib
```

### Playwright cannot find Chromium

``` bash
python -m playwright install chromium
```

### The script cannot find `div.novel-text`

Run with the visible browser:

``` bash
python chikari_to_epub.py --show-browser
```

Check whether the page loaded and whether the chapter text is still
inside an element with the class `novel-text`. The script deliberately
avoids generic page containers because they may include comments or
unrelated text.

### Resume does not continue where expected

Confirm that `chapters/progress.json` exists and that you are using the
same folder as the original download. Use `--resume` to continue from
the stored URL. Use `--start NUMBER` only when you intentionally want to
choose a starting chapter.

### Rebuild from saved files

``` bash
python chikari_to_epub.py --build-epub-only
```

If a chapter file referenced in the manifest is missing, the script
warns and skips it.

## Help

Show all available command-line options:

``` bash
python chikari_to_epub.py --help
```
