"""
collego.edu.tw 爬蟲 v2 — 結構化版本
依真實 HTML 結構精確提取各區塊
輸出：data/collego_ncu.json
"""

import argparse
import asyncio
import json
import re
from pathlib import Path

# ── 設定 ─────────────────────────────────────────────
BASE_URL = "https://collego.edu.tw"
TARGET_SCHOOL = "國立中央大學"
SEARCH_URL = f'{BASE_URL}/Login/Search?t=%22%E5%9C%8B%E7%AB%8B%E4%B8%AD%E5%A4%AE%E5%A4%A7%E5%AD%B8%22'
OUTPUT_PATH = Path(__file__).parent.parent.parent / "data" / "collego_ncu.json"
OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)


def clean(t: str) -> str:
    return re.sub(r"\s+", " ", t or "").strip()


# ── 步驟 1：收集所有 dept_id ──────────────────────────
async def collect_all_dept_ids(page) -> list[dict]:
    print(f"[1] 前往搜尋頁並收集所有系所...")
    await page.goto(SEARCH_URL, wait_until="networkidle")
    await page.wait_for_timeout(4000)

    prev_count = 0
    for _ in range(20):
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await page.wait_for_timeout(2500)
        count = await page.evaluate(
            "() => document.querySelectorAll('a[href*=\"DepartmentIntro\"]').length"
        )
        print(f"  scroll: {count} 個系所")
        if count == prev_count:
            break
        prev_count = count

    links = await page.evaluate("""
        () => Array.from(document.querySelectorAll('a[href*="DepartmentIntro"]'))
            .map(a => ({ text: a.innerText.trim(), href: a.href }))
    """)

    depts = []
    seen = set()
    for l in links:
        m = re.search(r"dept_id=([A-Za-z0-9]+)", l["href"])
        if m and m.group(1) not in seen:
            seen.add(m.group(1))
            name = l["text"].replace(TARGET_SCHOOL, "").strip()
            depts.append({"dept_id": m.group(1), "dept_name": name, "url": l["href"]})

    print(f"  共 {len(depts)} 個系所")
    return depts


# ── 步驟 2：點擊 tab ──────────────────────────────────
async def click_tab(page, tab_index: int, pane_id: str):
    """點擊第 tab_index 個 tab，回傳對應 pane element"""
    tabs = await page.query_selector_all("ul.nav-tabs li a")
    if tab_index < len(tabs):
        await tabs[tab_index].click()
        await page.wait_for_timeout(1500)
    pane = await page.query_selector(f"#{pane_id}")
    return pane


# ── 提取：學系介紹 (tab1default) ─────────────────────
async def extract_dept_intro(pane) -> dict:
    """
    結構：
      h3 學系特色  → p.card-text
      h3 學科意涵  → p.card-text  + 可選下載連結
      h3 學習方法  → owl-carousel items (圖+說明文字)
    """
    result = {}

    # ── 學系特色 ──
    result["學系特色"] = await pane.evaluate("""
        (el) => {
            const h = Array.from(el.querySelectorAll('h3')).find(h => h.innerText.includes('學系特色'));
            if (!h) return '';
            const row = h.closest('.row') || h.closest('.col-padding');
            const p = row ? row.querySelector('p.card-text') : null;
            return p ? p.innerText.trim() : '';
        }
    """)

    # ── 學科意涵 ──
    result["學科意涵"] = await pane.evaluate("""
        (el) => {
            const h = Array.from(el.querySelectorAll('h3')).find(h => h.innerText.includes('學科意涵'));
            if (!h) return '';
            const row = h.closest('.row') || h.closest('.col-padding');
            const p = row ? row.querySelector('p.card-text') : null;
            return p ? p.innerText.trim() : '';
        }
    """)

    # ── 學習方法（owl carousel 的每個非複製 item） ──
    # HTML：div.owl-item:not(.cloned) > div.item > div.card > div.card-body > p.card-text
    learning_items = await pane.evaluate("""
        (el) => {
            // 只取非 cloned 的 owl-item，避免重複
            const items = el.querySelectorAll('.old.owl-carousel .owl-item:not(.cloned)');
            if (!items.length) {
                // 備用：找所有 .item
                return Array.from(el.querySelectorAll('.old.owl-carousel .item'))
                    .map(it => {
                        const p = it.querySelector('.card-body p.card-text');
                        return p ? p.innerText.trim() : '';
                    })
                    .filter(t => t.length > 1);
            }
            return Array.from(items).map(it => {
                // p.card-text 第一個是主說明，後面可能有圖解說明
                const paras = it.querySelectorAll('.card-body p.card-text');
                const main = paras[0] ? paras[0].innerText.trim() : '';
                const caption = paras[1] ? paras[1].innerText.trim() : '';
                return caption ? `${main}（${caption}）` : main;
            }).filter(t => t.length > 1);
        }
    """)
    result["學習方法"] = learning_items

    # 清除空值
    return {k: v for k, v in result.items() if v}


# ── 提取：課程資訊 (tab2default) ─────────────────────
async def extract_course_info(pane) -> dict:
    """
    結構：
      h3 核心課程地圖  → ul.tree > li(年級) > ul > li(課名)
      h3 專業選修課程  → ul.tree > li(類別) > ul > li(課名)
      下方連結         → 完整課程地圖 URL
    """
    result = {}

    # ── 核心課程地圖（必修） ──
    core = await pane.evaluate("""
        (el) => {
            const h = Array.from(el.querySelectorAll('h3')).find(h => h.innerText.includes('核心課程'));
            if (!h) return {};
            const container = h.closest('.col-md-6') || h.closest('.col-padding') || h.closest('.row');
            if (!container) return {};
            const result = {};
            container.querySelectorAll('ul.tree > li').forEach(yearLi => {
                // yearLi.childNodes 第一個 textNode 是年級
                const yearText = yearLi.childNodes[0] ? yearLi.childNodes[0].textContent.trim() : '';
                if (!yearText) return;
                const courses = Array.from(yearLi.querySelectorAll('ul > li'))
                    .map(li => li.innerText.trim())
                    .filter(t => t.length > 0);
                if (yearText && courses.length > 0) {
                    result[yearText] = courses;
                }
            });
            return result;
        }
    """)
    if core:
        result["核心課程"] = core

    # ── 專業選修課程 ──
    elective = await pane.evaluate("""
        (el) => {
            const h = Array.from(el.querySelectorAll('h3')).find(h => h.innerText.includes('專業選修'));
            if (!h) return {};
            const container = h.closest('.col-md-6') || h.closest('.col-padding') || h.closest('.row');
            if (!container) return {};
            const result = {};
            container.querySelectorAll('ul.tree > li').forEach(catLi => {
                const catText = catLi.childNodes[0] ? catLi.childNodes[0].textContent.trim() : '';
                if (!catText) return;
                const courses = Array.from(catLi.querySelectorAll('ul > li'))
                    .map(li => li.innerText.trim())
                    .filter(t => t.length > 0);
                if (catText && courses.length > 0) {
                    result[catText] = courses;
                }
            });
            return result;
        }
    """)
    if elective:
        result["專業選修課程"] = elective

    # ── 完整課程地圖連結 ──
    map_link = await pane.evaluate("""
        (el) => {
            const links = Array.from(el.querySelectorAll('a[href]'));
            const courseMapLink = links.find(a =>
                a.innerText.includes('課程地圖') ||
                a.innerText.includes('完整課程') ||
                a.href.includes('course')
            );
            return courseMapLink ? courseMapLink.href : '';
        }
    """)
    if map_link:
        result["完整課程地圖連結"] = map_link

    return result


# ── 提取：生涯進路 (tab3default) ─────────────────────
async def extract_career_path(pane) -> dict:
    """
    結構：
      h3 適合從事工作 → ul > li > h4(職稱) + p.text-muted(說明)
      h3 系友生涯發展 → owl-carousel (校友故事)
    """
    result = {}

    # ── 適合從事工作 ──
    jobs = await pane.evaluate("""
        (el) => {
            const h = Array.from(el.querySelectorAll('h3')).find(h => h.innerText.includes('適合從事工作'));
            if (!h) return [];
            const container = h.closest('.col-padding') || h.closest('.row');
            if (!container) return [];
            const result = [];
            container.querySelectorAll('ul > li > h4').forEach(h4 => {
                const title = h4.innerText.trim();
                const desc = h4.closest('li') ?
                    (h4.closest('li').querySelector('p.text-muted') || {}).innerText || '' : '';
                if (title && title !== '如右說明') {
                    result.push({ 職稱: title, 說明: desc.trim() });
                } else if (title === '如右說明') {
                    // 部分系所用 '如右說明'，說明文字直接在 p
                    const desc2 = h4.closest('li') ?
                        (h4.closest('li').querySelector('p.text-muted') || {}).innerText || '' : '';
                    if (desc2.trim()) result.push({ 職稱: desc2.trim(), 說明: '' });
                }
            });
            return result;
        }
    """)
    if jobs:
        result["適合從事工作"] = jobs

    # ── 系友生涯發展（校友）──
    alumni = await pane.evaluate("""
        (el) => {
            const h = Array.from(el.querySelectorAll('h3')).find(h => h.innerText.includes('系友') || h.innerText.includes('生涯發展'));
            if (!h) return [];
            const container = h.closest('.row') || h.closest('.col-padding');
            if (!container) return [];
            const items = [];
            // 找所有 owl-item 中的文字
            container.querySelectorAll('.owl-item').forEach(item => {
                const texts = Array.from(item.querySelectorAll('p, span, .desc'))
                    .map(e => e.innerText.trim())
                    .filter(t => t.length > 3 && !t.includes('未上傳'));
                if (texts.length) items.push(texts.join(' '));
            });
            // 備用：找 p 段落
            if (!items.length) {
                container.querySelectorAll('p').forEach(p => {
                    const t = p.innerText.trim();
                    if (t.length > 3) items.push(t);
                });
            }
            return items;
        }
    """)
    if alumni:
        result["系友生涯發展"] = alumni

    return result


# ── 提取：能力特質 (tab4default) ─────────────────────
async def extract_ability(pane) -> dict:
    """
    HTML 結構：
      span.progress-type → 能力名稱（含說明）
      div.progress > div.progress-bar[aria-valuenow] → 百分比
    排列方式：span → div.progress → span → div.progress → ...
    """
    abilities = await pane.evaluate("""
        (el) => {
            const items = [];
            // span.progress-type 與後面的 div.progress 成對出現
            const spans = el.querySelectorAll('span.progress-type');
            spans.forEach(span => {
                const raw = span.innerText.trim();
                // 格式：「能力名稱：說明文字」
                const colonIdx = raw.indexOf('：');
                const name = colonIdx > -1 ? raw.substring(0, colonIdx).trim() : raw.split('，')[0].trim();
                const desc = colonIdx > -1 ? raw.substring(colonIdx + 1).trim() : '';

                // 找緊接在後的 progress-bar
                let next = span.nextElementSibling;
                let pct = null;
                while (next && next.tagName !== 'SPAN') {
                    const bar = next.querySelector('.progress-bar[aria-valuenow]');
                    if (bar) {
                        pct = parseInt(bar.getAttribute('aria-valuenow'));
                        break;
                    }
                    next = next.nextElementSibling;
                }
                if (name.length > 1) {
                    items.push({ 能力: name, 說明: desc, 比例_百分比: pct });
                }
            });
            return items;
        }
    """)

    return {"多元能力": abilities} if abilities else {}


# ── 提取：基本資訊 ────────────────────────────────────
async def extract_basic_info(page) -> dict:
    """從頁面頂部提取校系網站、電話、email、地址"""
    return await page.evaluate("""
        () => {
            const info = { 校系網站: '', 電話: '', email: '', 地址: '' };

            // 校系網站
            const websiteLink = Array.from(document.querySelectorAll('a'))
                .find(a => a.innerText.trim() === '校系網站' || a.innerText.trim() === '系所官網');
            if (websiteLink) info.校系網站 = websiteLink.href;

            // 電話
            const body = document.body.innerText;
            const phoneM = body.match(/\(\d{2,3}\)\d{7,8}(?:分機\d+)?/);
            if (phoneM) info.電話 = phoneM[0];

            // email
            const emailM = body.match(/[\w.+-]+@[\w.]+\.[\w.]{2,}/);
            if (emailM) info.email = emailM[0];

            // 地址（找包含「市」「區」「路」的文字）
            const addrM = body.match(/\d{3}[台臺]?[\w市縣]{2,4}[\w區鄉鎮市]{2,4}[\w路街道]{2,5}\d+號/);
            if (addrM) info.地址 = addrM[0];

            return info;
        }
    """)


# ── 步驟 2：爬取單一系所 ──────────────────────────────
async def scrape_department(page, dept: dict) -> dict:
    dept_id = dept["dept_id"]
    dept_name = dept["dept_name"]
    dept_url = dept["url"] if dept["url"].startswith("http") else BASE_URL + dept["url"]

    result = {
        "dept_id": dept_id,
        "dept_name": dept_name,
        "dept_url": dept_url,
    }

    try:
        await page.goto(dept_url, wait_until="networkidle", timeout=40000)
        await page.wait_for_timeout(2000)

        body_text = await page.inner_text("body")
        if "系統發生不可預期錯誤" in body_text or "Error 500" in body_text:
            result["error"] = "500"
            return result

        # 基本資訊
        result.update(await extract_basic_info(page))

        # 學系介紹 (tab index 0, pane tab1default)
        pane1 = await click_tab(page, 0, "tab1default")
        result["學系介紹"] = await extract_dept_intro(pane1) if pane1 else {}

        # 課程資訊 (tab index 1, pane tab2default)
        pane2 = await click_tab(page, 1, "tab2default")
        result["課程資訊"] = await extract_course_info(pane2) if pane2 else {}

        # 生涯進路 (tab index 2, pane tab3default)
        pane3 = await click_tab(page, 2, "tab3default")
        result["生涯進路"] = await extract_career_path(pane3) if pane3 else {}

        # 能力特質 (tab index 3, pane tab4default)
        pane4 = await click_tab(page, 3, "tab4default")
        result["能力特質"] = await extract_ability(pane4) if pane4 else {}

    except Exception as e:
        result["error"] = str(e)[:120]

    return result


# ── 主流程 ────────────────────────────────────────────
def require_explicit_authorization() -> None:
    parser = argparse.ArgumentParser(
        description="舊版 ColleGo! 研究爬蟲；不得接入正式服務"
    )
    parser.add_argument(
        "--confirm-authorized",
        action="store_true",
        help="確認已取得涵蓋重製、索引及公開展示的授權",
    )
    args = parser.parse_args()
    if not args.confirm_authorized:
        raise SystemExit(
            "此舊版爬蟲已停用。只有在取得可涵蓋重製、建立檢索索引與公開展示的明確授權後，"
            "才可加上 --confirm-authorized 執行；輸出仍不得直接接回正式索引。"
        )


async def main():
    from playwright.async_api import async_playwright

    print("=" * 60)
    print(f"  collego.edu.tw 爬蟲 v2 — {TARGET_SCHOOL}")
    print("=" * 60)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, slow_mo=100)
        ctx = await browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
        )
        page = await ctx.new_page()

        depts = await collect_all_dept_ids(page)
        if not depts:
            print("✗ 無法取得系所清單")
            await browser.close()
            return

        print(f"\n[2] 開始爬取 {len(depts)} 個系所...")
        print("-" * 60)

        all_results = []
        for i, dept in enumerate(depts, 1):
            print(f"[{i:3d}/{len(depts)}] {dept['dept_name']} ({dept['dept_id']})", end=" ")
            result = await scrape_department(page, dept)
            all_results.append(result)
            status = "✓" if not result.get("error") else f"✗ {result.get('error','')}"
            print(status)
            await page.wait_for_timeout(500)

        print(f"\n[3] 儲存至 {OUTPUT_PATH}...")
        with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
            json.dump(all_results, f, ensure_ascii=False, indent=2)

        success = sum(1 for r in all_results if not r.get("error"))
        print(f"\n{'='*60}")
        print(f"  完成！{success}/{len(all_results)} 個系所成功")
        print(f"  輸出：{OUTPUT_PATH}")
        print("=" * 60)

        await browser.close()


if __name__ == "__main__":
    require_explicit_authorization()
    asyncio.run(main())
