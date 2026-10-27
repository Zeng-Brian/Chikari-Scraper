
#!/usr/bin/env python3
"""Download Beyond the Timescape, save chapters locally, resume, and build EPUB."""

import argparse
import json
import re
import sys
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
from ebooklib import epub
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError


DEFAULT_URL = "https://chikari.moe/novels/beyond-the-timescape/1"
SITE_HOST = "chikari.moe"


def clean_text(text):
    return re.sub(r"\s+", " ", text or "").strip()


def atomic_json(path, data):
    """Write JSON atomically so an interrupted write won't corrupt progress."""
    path = Path(path)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    temp.replace(path)


def load_json(path, default):
    path = Path(path)
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def extract_body(page):
    """Use only the known chapter-text container, not generic page containers."""
    candidates = []

    for element in page.locator("div.novel-text").all():
        try:
            info = element.evaluate("""el => ({
                text: el.innerText || "",
                paragraphs: [...el.querySelectorAll("p")]
                    .map(p => (p.innerText || "").trim())
                    .filter(Boolean)
            })""")

            text = clean_text(info["text"])
            paragraphs = info["paragraphs"]

            if len(text) < 200 or not paragraphs:
                continue
            if sum(len(p) for p in paragraphs) < 150:
                continue

            candidates.append((sum(len(p) for p in paragraphs), element))
        except Exception:
            continue

    if not candidates:
        return None

    candidates.sort(key=lambda x: x[0], reverse=True)
    return candidates[0][1]


def extract_title(page):
    """Prefer the chapter title in the reader header."""
    selectors = [
        "header.mx-auto.max-w-3xl.px-4.pt-6",
        "header[class*='max-w-3xl'][class*='pt-6']",
    ]

    for selector in selectors:
        try:
            for element in page.locator(selector).all():
                for line in element.inner_text(timeout=2000).splitlines():
                    line = clean_text(line)
                    if not line:
                        continue

                    match = re.search(
                        r"\b((?:Chapter\s+\d+|Prologue|Epilogue)"
                        r"\s*[:：—-]\s*.+)",
                        line,
                        re.IGNORECASE,
                    )
                    if match:
                        return match.group(1).strip()

        except Exception:
            continue

    # Fallback to headings, but not site titles or comments headings.
    for selector in ("h1", "h2", "h3"):
        try:
            for element in page.locator(selector).all():
                title = clean_text(element.inner_text(timeout=1000))
                if not title or len(title) > 180:
                    continue
                if re.fullmatch(
                    r"comments?\s*(?:\(\s*\d+\s*\)|\d+)?",
                    title,
                    re.IGNORECASE,
                ):
                    continue
                if re.search(
                    r"\b(?:Chapter\s+\d+|Prologue|Epilogue)\b",
                    title,
                    re.IGNORECASE,
                ):
                    return title
        except Exception:
            continue

    match = re.search(r"/(\d+)/?$", urlparse(page.url).path)
    return f"Chapter {match.group(1)}" if match else "Untitled Chapter"


def clean_body_html(element):
    """Clean the selected chapter body without reading comments elsewhere."""
    soup = BeautifulSoup(
        element.evaluate("(el) => el.innerHTML"),
        "html.parser",
    )

    for node in soup.select(
        "script, style, nav, form, button, input, textarea, "
        "select, option, iframe, noscript, svg"
    ):
        node.decompose()

    # Remove comment widgets if one is unexpectedly nested inside the body.
    for node in list(soup.find_all(True)):
        classes = node.get("class", [])
        if isinstance(classes, str):
            classes = classes.split()

        marker = " ".join([str(node.get("id", ""))] + classes)
        if re.search(r"comment|discussion|repl(?:y|ies)", marker, re.I):
            node.decompose()

    return str(soup)


def find_next_url(page, current_url):
    """Find a next-chapter link; use the numeric URL as a fallback."""
    try:
        candidates = []

        for anchor in page.locator("a[href]").all():
            try:
                label = clean_text(anchor.inner_text(timeout=500))
                aria = anchor.get_attribute("aria-label") or ""
                title = anchor.get_attribute("title") or ""
                combined = " ".join((label, aria, title))

                if not re.search(
                    r"\b(next|next chapter|following chapter|continue)\b",
                    combined,
                    re.I,
                ):
                    continue

                href = anchor.get_attribute("href")
                if not href:
                    continue

                absolute = urljoin(current_url, href)
                parsed = urlparse(absolute)

                if parsed.netloc and parsed.netloc != SITE_HOST:
                    continue
                if absolute.rstrip("/") == current_url.rstrip("/"):
                    continue

                score = 0 if re.search(r"next chapter", combined, re.I) else 1
                candidates.append((score, absolute))
            except Exception:
                continue

        if candidates:
            candidates.sort(key=lambda item: item[0])
            return candidates[0][1]

    except Exception:
        pass

    parsed = urlparse(current_url)
    match = re.search(r"/(\d+)/?$", parsed.path)

    if match:
        path = parsed.path[:match.start(1)] + str(int(match.group(1)) + 1)
        return parsed._replace(path=path).geturl()

    return None


def chapter_filename(number):
    return f"chapter_{number:05d}.html"


def save_chapter(folder, number, url, title, body_html):
    """Save chapter content first, then update the manifest."""
    folder.mkdir(parents=True, exist_ok=True)

    filename = chapter_filename(number)
    path = folder / filename
    temp = path.with_suffix(".html.tmp")

    document = (
        "<!DOCTYPE html><html><head><meta charset='utf-8'>"
        f"<title>{title}</title></head><body>{body_html}</body></html>"
    )

    temp.write_text(document, encoding="utf-8")
    temp.replace(path)

    return {
        "number": number,
        "url": url,
        "title": title,
        "file": filename,
    }


def download_chapters(args, folder, manifest, state):
    folder.mkdir(parents=True, exist_ok=True)
    state_path = folder / "progress.json"
    manifest_path = folder / "manifest.json"

    if args.resume:
        current_url = state.get("next_url")
        number = int(state.get("next_number", 1))
        if not current_url:
            print("No pending URL in progress.json; nothing to resume.")
            return manifest, state
        print(f"Resuming at chapter slot {number}: {current_url}")
    else:
        current_url = args.url
        number = args.start_number or 1

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not args.show_browser)
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/128.0.0.0 Safari/537.36"
            )
        )
        page = context.new_page()
        page.set_default_timeout(15000)

        try:
            for _ in range(args.max_chapters):
                if not current_url:
                    print("No next chapter URL found.")
                    break

                parsed = urlparse(current_url)
                if parsed.netloc and parsed.netloc != SITE_HOST:
                    print(f"Stopping: unexpected host: {parsed.netloc}")
                    break

                print(f"\n[{number}] {current_url}")

                # Retry navigation up to three times. If all fail, preserve
                # this URL in progress.json so the next run retries it.
                loaded = False
                for attempt in range(1, 4):
                    try:
                        page.goto(
                            current_url,
                            wait_until="domcontentloaded",
                            timeout=45000,
                        )
                        try:
                            page.locator("div.novel-text").first.wait_for(
                                state="attached", timeout=15000
                            )
                        except PlaywrightTimeoutError:
                            pass

                        page.wait_for_timeout(1200)
                        loaded = True
                        break
                    except Exception as exc:
                        print(f"  Attempt {attempt}/3 failed: {exc}")
                        if attempt < 3:
                            time.sleep(2 * attempt)

                if not loaded:
                    print("  Stopping. Run with --resume to retry this chapter.")
                    atomic_json(state_path, {
                        "next_url": current_url,
                        "next_number": number,
                    })
                    break

                body = extract_body(page)
                if body is None:
                    print(
                        "  No usable div.novel-text found. "
                        "Stopping to avoid saving comments."
                    )
                    atomic_json(state_path, {
                        "next_url": current_url,
                        "next_number": number,
                    })
                    break

                title = extract_title(page)
                body_html = clean_body_html(body)
                plain = clean_text(
                    BeautifulSoup(body_html, "html.parser").get_text(" ", strip=True)
                )

                if len(plain) < 150:
                    print("  Body is too short; stopping without saving.")
                    atomic_json(state_path, {
                        "next_url": current_url,
                        "next_number": number,
                    })
                    break

                next_url = find_next_url(page, current_url)

                # Save the chapter to disk before marking it complete.
                entry = save_chapter(folder, number, current_url, title, body_html)

                # Replace an existing manifest entry for this number if needed.
                manifest = [x for x in manifest if x.get("number") != number]
                manifest.append(entry)
                manifest.sort(key=lambda x: x["number"])
                atomic_json(manifest_path, {"chapters": manifest})

                # Progress is advanced only after the chapter and manifest
                # have both been written successfully.
                state = {
                    "next_url": next_url,
                    "next_number": number + 1,
                }
                atomic_json(state_path, state)

                print(f"  Saved {title} ({len(plain)} characters)")

                if not next_url:
                    print("  No next URL found; download finished.")
                    break

                current_url = next_url
                number += 1
                time.sleep(args.delay)

        finally:
            context.close()
            browser.close()

    return manifest, state


def build_epub(folder, manifest, output_path, title, author, language):
    """Build an EPUB entirely from locally saved chapter files."""
    if not manifest:
        raise RuntimeError("No saved chapters found in the manifest.")

    book = epub.EpubBook()
    book.set_identifier("beyond-the-timescape-archive")
    book.set_title(title)
    book.set_language(language)
    if author:
        book.add_author(author)

    css = epub.EpubItem(
        uid="style",
        file_name="style/book.css",
        media_type="text/css",
        content="""
            body { font-family: serif; line-height: 1.55; }
            h1 { text-align: center; }
            p { margin: 0.6em 0; text-indent: 1.2em; }
        """,
    )
    book.add_item(css)

    toc = []
    spine = ["nav"]

    for entry in sorted(manifest, key=lambda x: x["number"]):
        path = folder / entry["file"]
        if not path.exists():
            print(f"Warning: missing chapter file: {path}")
            continue

        saved_html = BeautifulSoup(
            path.read_text(encoding="utf-8"), "html.parser"
        )
        body = saved_html.body
        if body is None:
            body_content = str(saved_html)
        else:
            body_content = "".join(str(child) for child in body.contents)

        chapter_title = entry.get("title", f"Chapter {entry['number']}")
        safe_title = (
            chapter_title.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
        )

        item = epub.EpubHtml(
            title=chapter_title,
            file_name=f"chapter_{entry['number']:05d}.xhtml",
            lang=language,
        )
        item.content = (
            "<html><head>"
            f"<title>{safe_title}</title>"
            '<link rel="stylesheet" type="text/css" href="style/book.css"/>'
            "</head><body>"
            f"<h1>{safe_title}</h1>{body_content}"
            "</body></html>"
        )

        book.add_item(item)
        toc.append(item)
        spine.append(item)

    if not toc:
        raise RuntimeError("No readable chapter files found.")

    book.toc = tuple(toc)
    book.spine = spine
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    epub.write_epub(str(output_path), book, {})
    print(f"Created {output_path.resolve()} with {len(toc)} chapters.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=DEFAULT_URL,
                        help="Starting chapter URL")
    parser.add_argument("--start", type=int, dest="start_number",
                        help="Start at a numbered chapter URL")
    parser.add_argument("--max-chapters", type=int, default=4000,
                        help="Maximum chapters to download in this run")
    parser.add_argument("--delay", type=float, default=1.2,
                        help="Delay between chapter requests")
    parser.add_argument("--resume", action="store_true",
                        help="Resume from progress.json")
    parser.add_argument("--build-epub-only", action="store_true",
                        help="Rebuild EPUB from saved chapters without downloading")
    parser.add_argument("--output", default="Beyond the Timescape.epub",
                        help="Output EPUB filename")
    parser.add_argument("--title", default="Beyond the Timescape")
    parser.add_argument("--author", default="Er Gen")
    parser.add_argument("--language", default="en")
    parser.add_argument("--show-browser", action="store_true",
                        help="Show Chromium")
    args = parser.parse_args()

    folder = Path(args.title)
    manifest_path = folder / "manifest.json"
    state_path = folder / "progress.json"

    manifest_data = load_json(manifest_path, {"chapters": []})
    manifest = manifest_data.get("chapters", [])
    state = load_json(state_path, {})

    if args.build_epub_only:
        build_epub(
            folder, manifest, args.output, args.title, args.author, args.language
        )
        return

    if args.start_number is not None:
        parsed = urlparse(args.url)
        path = re.sub(r"/\d+/?$", f"/{args.start_number}", parsed.path)
        if path == parsed.path:
            path = parsed.path.rstrip("/") + f"/{args.start_number}"
        args.url = parsed._replace(path=path).geturl()

    manifest, state = download_chapters(args, folder, manifest, state)

    if manifest:
        build_epub(
            folder, manifest, args.output, args.title, args.author, args.language
        )
    else:
        print("No chapters are saved yet; no EPUB was created.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nInterrupted. Saved chapters and progress are preserved.")
        raise SystemExit(130)

