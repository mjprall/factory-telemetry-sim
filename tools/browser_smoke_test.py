"""Headless-Chrome smoke test (optional, needs: pip install playwright).

Serve the site first (python3 -m http.server 8765), then:
    python tools/browser_smoke_test.py [URL]
Boots the page, clicks fault buttons, prints statuses/log/console errors and
refreshes screenshots/. Uses the system Chrome at /usr/bin/google-chrome."""
import sys, time
from playwright.sync_api import sync_playwright

URL = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8765/"
OUT = str(__import__("pathlib").Path(__file__).resolve().parent.parent / "screenshots")
errors, logs = [], []
with sync_playwright() as p:
    b = p.chromium.launch(executable_path="/usr/bin/google-chrome", headless=True,
                          args=["--no-sandbox", "--disable-gpu"])
    page = b.new_page(viewport={"width": 1440, "height": 1000}, device_scale_factor=1)
    page.on("console", lambda m: (logs.append(f"{m.type}: {m.text}"),
                                  errors.append(m.text) if m.type == "error" else None))
    page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
    page.on("requestfailed", lambda r: errors.append(f"requestfailed: {r.url} {r.failure}"))
    sizes = []
    def on_resp(r):
        try:
            sizes.append(int(r.headers.get("content-length") or 0))
        except Exception:
            pass
    page.on("response", on_resp)
    t0 = time.time()
    page.goto(URL)
    page.wait_for_selector("body.py-ready", timeout=120_000)
    print(f"python ready in {time.time()-t0:.1f}s")
    page.wait_for_timeout(3000)
    # Inject faults via real clicks.
    page.select_option("#speed", "5")
    page.click(".fault-btn[data-fault=bearing_wear]")             # CNC-01 (default target)
    page.click(".machine[data-id='PRS-03']")
    page.click(".fault-btn[data-fault=overheat]")
    page.click(".machine[data-id='CMP-04']")
    page.click(".fault-btn[data-fault=dropout]")
    page.click(".machine[data-id='CNC-02']")
    page.click(".fault-btn[data-fault=network_delay]")
    page.wait_for_timeout(5000)
    page.click(".fault-btn[data-fault=spike]")   # CNC-02 still selected
    page.wait_for_timeout(5000)
    statuses = page.eval_on_selector_all(".machine", "els => els.map(e => [e.dataset.id, e.dataset.status])")
    print("statuses:", statuses)
    print("log items:", page.eval_on_selector_all("#log li", "els => els.slice(0,12).map(e => e.innerText.replace(/\\n/g,' '))"))
    print("stats:", page.inner_text(".stats").replace("\n", " "))
    page.screenshot(path=f"{OUT}/dashboard-faults.png", full_page=True)
    # Mobile layout + source dialog
    page.set_viewport_size({"width": 390, "height": 844})
    page.wait_for_timeout(800)
    page.evaluate("window.scrollTo(0, 0)")
    page.wait_for_timeout(300)
    page.screenshot(path=f"{OUT}/mobile.png", full_page=False)
    page.click(".machine[data-id='CNC-01']")
    page.click("#btn-clear")
    page.wait_for_timeout(1500)
    print("after maintenance CNC-01:", page.get_attribute(".machine[data-id='CNC-01']", "data-status"))
    page.click("#view-source")
    page.wait_for_timeout(1500)
    print("source starts:", page.inner_text("#source-code")[:60].replace("\n", " | "))
    page.screenshot(path="/tmp/tsim-source.png")
    b.close()
print(f"transferred (content-length sum): {sum(sizes)/1e6:.1f} MB over {len(sizes)} responses")
print("console errors:", errors or "none")
print("\n".join(l for l in logs if not l.startswith("error"))[:1500])
