#!/usr/bin/env python3 
""" 
Brazos Valley Morning Brief — FREE VERSION 
────────────────────────────────────────── 
Runs every weekday morning via GitHub Actions. 
 
Cost: $0/month. 
  • Groq API (primary)    — free tier, Llama 3.3 70B, 14,400 req/day 
  • Google Gemini (fallback) — free tier, 1,500 req/day 
  • GitHub Actions        — free 
  • Gmail SMTP            — free 
  • RSS / web scraping    — free 
 
Fallback logic: 
  1. Try Groq first (faster, higher free limits) 
  2. If Groq fails or returns unparseable JSON → retry once 
  3. If still failing → switch to Gemini automatically 
  4. If both fail → deliver an error pitch so email still arrives 
""" 
 
import os 
import re 
import json 
import xml.etree.ElementTree as ET 
import smtplib 
import datetime 
import time 
import requests 
from email.mime.multipart import MIMEMultipart 
from email.mime.text import MIMEText 
from bs4 import BeautifulSoup 
import pytz 
 
# ── Optional imports — script handles missing keys gracefully ── 
try: 
    from groq import Groq 
    GROQ_AVAILABLE = True 
except ImportError: 
    GROQ_AVAILABLE = False 
 
try: 
    from google import genai as google_genai 
    GEMINI_AVAILABLE = True 
except ImportError: 
    GEMINI_AVAILABLE = False 
 
 
# ───────────────────────────────────────────────────────────── 
# CONFIG  (all values from GitHub Secrets) 
# ───────────────────────────────────────────────────────────── 
GROQ_API_KEY       = os.environ.get("GROQ_API_KEY", "") 
GEMINI_API_KEY     = os.environ.get("GEMINI_API_KEY", "") 
GMAIL_ADDRESS      = os.environ["GMAIL_ADDRESS"] 
GMAIL_APP_PASSWORD = os.environ["GMAIL_APP_PASSWORD"] 
RECIPIENT_EMAIL    = os.environ["RECIPIENT_EMAIL"] 
EXTRA_EMAILS       = os.environ.get("EXTRA_RECIPIENT_EMAILS", "") 
 
CT          = pytz.timezone("US/Central") 
NOW         = datetime.datetime.now(CT) 
TODAY       = NOW.strftime("%A, %B %-d, %Y") 
TODAY_SHORT = NOW.strftime("%Y-%m-%d") 
 
COVERAGE_COUNTIES = [ 
    "Brazos", "Burleson", "Grimes", "Walker", 
    "Madison", "Leon", "Washington", "Milam", "Robertson", 
] 
COVERAGE_CITIES = ["Bryan", "College Station"] 
 
LOCAL_OUTLETS = [ 
    "KBTX", "kbtx.com", "The Eagle", "theeagle.com", 
    "WTAW", "wtaw.com", "Community Impact", 
] 
 
HEADERS = { 
    "User-Agent": ( 
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) " 
        "AppleWebKit/537.36 (KHTML, like Gecko) " 
        "Chrome/124.0.0.0 Safari/537.36" 
    ) 
} 
 
URGENCY_COLOR = { 
    "TODAY":      "#c0392b", 
    "THIS WEEK":  "#e67e22", 
    "ENTERPRISE": "#27ae60", 
} 
CATEGORY_EMOJI = { 
    "ACCOUNTABILITY": " ", 
    "GOVERNMENT":     " ", 
    "PUBLIC SAFETY":  " ", 
    "EDUCATION":      " ", 
    "ENVIRONMENT":    " ", 
    "ECONOMY":        " ", 
    "SYSTEM":         " ", 
} 
 
# Shared prompt used by both AI providers 
PITCH_PROMPT_TEMPLATE = """Today is {today}. 
 
You are a veteran investigative producer helping a News Director at KBTX 
(CBS affiliate, Bryan-College Station, TX) identify ORIGINAL story 
pitches. 
Stories must NOT already be covered by: {outlets} 
 
Coverage area: {counties}; City of Bryan; City of College Station; 
Texas A&M University; Blinn College. 
─── GOVERNMENT AGENDA DATA ─────────────────────────────────────── 
{agenda_block} 
─── STATE & FEDERAL SOURCES (RSS feeds + agency pages) ─────────── 
{state_federal_data} 
────────────────────────────────────────────────────────────────── 
TASK: Find up to 8 story pitches. Every pitch must meet ALL of these 
standards: 
✓ SPECIFIC — includes real names, dollar amounts, vote counts, or dates 
from the data 
✓ ORIGINAL — not already covered by the local outlets above 
✓ LOCAL IMPACT — explains exactly which residents, neighborhoods, or 
institutions are affected 
✓ SOURCED — tied to a specific item in the data above, with a URL if one 
appears in the data 
HARD RULES — violating any of these disqualifies the pitch: 
✗ NO vague language: forbidden phrases include "may impact," "could 
affect," "this story explores," 
"potential effects," "raises questions," "worth monitoring" 
✗ NO routine items: contract renewals, budget amendments, standard 
personnel votes 
✗ NO stories without a named person, named place, or specific dollar 
figure 
✗ NO pitches that could apply to any city in Texas — must be 
specifically local 
ANGLE FIELD STANDARD — write the angle the way a news director would say 
it out loud 
to a reporter in a morning meeting. It should sound like: 
"Governor Abbott just appointed three new A&M regents — including [NAME] 
— two weeks 
before the board votes on the presidential search. Get faculty senate 
reaction and find 
out whether these appointments shift the balance of power." 
NOT like: "The recent appointments may impact local education policies 
and decisions." 
SOURCE FIELD — include the URL from the RSS data if one is present. If no 
URL is 
available, name the specific document or agenda item (e.g. "Brazos County 
Commissioners 
Court agenda item 4B, April 7, 2026" or "TCEQ enforcement order #12345"). 
Return ONLY a valid JSON array. No preamble. No explanation. No markdown 
fences. 
Start your response with [ and end with ]. 
Each object must have exactly these fields: 
{{ 
  "headline":  "Broadcast-style slug, present tense, 6-8 words, specific 
not generic", 
  "angle":     "2-3 sentences written like a news director briefing a 
reporter. Must include at least one specific name, number, or date from 
the source data.", 
  "source":    "URL if available, otherwise specific document/agenda item 
name and date.", 
  "next_step": "One concrete reporting action with a specific person or 
office to contact.", 
  "urgency":   "TODAY | THIS WEEK | ENTERPRISE", 
  "category":  "ACCOUNTABILITY | GOVERNMENT | PUBLIC SAFETY | EDUCATION | 
ENVIRONMENT | ECONOMY" 
}} 
 
Sort pitches: TODAY first, then THIS WEEK, then ENTERPRISE.""" 
 
 
# ───────────────────────────────────────────────────────────── 
# AGENDA FETCHERS 
# ───────────────────────────────────────────────────────────── 
 
def fetch_boarddocs(base_url: str, org_name: str) -> str: 
    try: 
        r = requests.get( 
            f"{base_url}/BD-GetMeetingsList?open&limit=6", 
            headers=HEADERS, timeout=20 
        ) 
        r.raise_for_status() 
        meetings = r.json() 
        results = [] 
        for mtg in meetings[:4]: 
            unid  = mtg.get("unique") or mtg.get("unid") or 
mtg.get("uniqueid", "") 
            title = mtg.get("title", "Meeting") 
            date  = mtg.get("numberdate") or mtg.get("date", "") 
            if not unid: 
                continue 
            ir = requests.get( 
                f"{base_url}/BD-GetAgendaItems/{unid}?open", 
                headers=HEADERS, timeout=20 
            ) 
            if ir.status_code == 200: 
                items  = ir.json() 
                titles = [ 
                    i.get("title", "").strip() 
                    for i in items if i.get("title", "").strip() 
                ] 
                results.append( 
                    f"Meeting: {title} ({date})\nItems: {' | 
'.join(titles[:25])}" 
                ) 
        return "\n\n".join(results) if results else "No recent BoardDocs 
data." 
    except Exception as e: 
        return f"[BoardDocs error – {org_name}: {e}]" 
 
 
def fetch_novusagenda(url: str, org_name: str) -> str: 
    try: 
        r = requests.get(url, headers=HEADERS, timeout=20) 
        r.raise_for_status() 
        soup = BeautifulSoup(r.text, "html.parser") 
        rows = [] 
        for el in soup.select("table tr, .meeting-item, .ms-rtestate
field")[:20]: 
            text  = el.get_text(" ", strip=True) 
            if len(text) > 15: 
                links = [ 
                    a["href"] for a in el.find_all("a", href=True) 
                    if "agenda" in a.get_text().lower() or ".pdf" in 
a["href"].lower() 
                ] 
                rows.append(text[:300] + (f"  [agenda: {links[0]}]" if 
links else "")) 
        if rows: 
            return "\n".join(rows[:15]) 
        return re.sub(r"\s+", " ", soup.get_text(" ", strip=True))[:3000] 
    except Exception as e: 
        return f"[NovusAgenda error – {org_name}: {e}]" 
 
 
def fetch_boardbook(url: str, org_name: str) -> str: 
    try: 
        r = requests.get(url, headers=HEADERS, timeout=20) 
        r.raise_for_status() 
        soup = BeautifulSoup(r.text, "html.parser") 
        months = { 
            "January","February","March","April","May","June", 
            "July","August","September","October","November","December", 
        } 
        meetings = [] 
        for el in soup.select("li, tr, .meeting")[:30]: 
            text = el.get_text(" ", strip=True) 
            if len(text) > 15 and any(m in text for m in months): 
                meetings.append(text[:300]) 
        return "\n".join(meetings[:12]) if meetings else \ 
               re.sub(r"\s+", " ", soup.get_text(" ", strip=True))[:2500] 
    except Exception as e: 
        return f"[BoardBook error – {org_name}: {e}]" 
 
 
def fetch_laserfiche_cs(_org_name: str) -> str: 
    try: 
        r = requests.get( 
            "https://blog.cstx.gov/category/city-council/", 
            headers=HEADERS, timeout=20 
        ) 
        soup = BeautifulSoup(r.text, "html.parser") 
        posts = [] 
        for article in soup.select("article, .post")[:6]: 
            h     = article.find(["h1","h2","h3"]) 
            title = h.get_text(strip=True) if h else "" 
            body  = re.sub(r"\s+", " ", article.get_text(" ", 
strip=True))[:500] 
            posts.append(f"POST: {title}\n{body}") 
        if posts: 
            return "\n\n".join(posts) 
        r2 = requests.get( 
            "https://www.cstx.gov/your-government/agendas-and-minutes/", 
            headers=HEADERS, timeout=20 
        ) 
        soup2 = BeautifulSoup(r2.text, "html.parser") 
        return re.sub(r"\s+", " ", soup2.get_text(" ", 
strip=True))[:2500] 
    except Exception as e: 
        return f"[College Station fetch error: {e}]" 
 
 
def fetch_html_generic(url: str, org_name: str) -> str: 
    try: 
        r = requests.get(url, headers=HEADERS, timeout=20) 
        r.raise_for_status() 
        soup = BeautifulSoup(r.text, "html.parser") 
        for tag in soup(["script","style","nav","footer","header"]): 
            tag.decompose() 
        main = ( 
            soup.find("main") 
            or soup.find("article") 
            or soup.find("div", id=re.compile(r"content|main", re.I)) 
            or soup.find("div", class_=re.compile(r"content|main|body", 
re.I)) 
        ) 
        return re.sub(r"\s+", " ", (main or soup).get_text(" ", 
strip=True))[:3000] 
    except Exception as e: 
        return f"[HTML fetch error – {org_name}: {e}]" 
 
 
AGENDA_SOURCES = [ 
    {"name": "Brazos County Commissioners Court", 
     "type": "novusagenda", 
     "url":  "https://brazos.novusagenda.com/agendapublic/"}, 
    {"name": "City of Bryan City Council", 
     "type": "boarddocs", 
     "url":  "https://go.boarddocs.com/tx/cobtx/Board.nsf"}, 
    {"name": "City of College Station City Council", 
     "type": "laserfiche", 
     "url":  "https://opendoc.cstx.gov/WeblinkPublic/Browse.aspx" 
             "?id=1291301&dbid=0&repo=DOCUMENT-SERVER&cr=1"}, 
    {"name": "Bryan ISD Board of Trustees", 
     "type": "boardbook", 
     "url":  "https://meetings.boardbook.org/Public/Organization/2246"}, 
    {"name": "College Station ISD Board of Trustees", 
     "type": "html", 
     "url":  "https://www.csisd.org/our-district/board/meeting-dates/" 
             "agendas-minutes-meeting-videos"}, 
    {"name": "Texas A&M System Board of Regents (Regular)", 
     "type": "html", 
     "url":  "https://www.tamus.edu/regents/meetingmaterials/regular/"}, 
    {"name": "Texas A&M System Board of Regents (Special)", 
     "type": "html", 
     "url":  "https://www.tamus.edu/regents/meetingmaterials/special
board/"}, 
    {"name": "Blinn College Board of Trustees", 
     "type": "boardbook", 
     "url":  "https://meetings.boardbook.org/Public/Organization/1319"}, 
] 
 
 
def fetch_all_agendas() -> dict: 
    results = {} 
    for src in AGENDA_SOURCES: 
        name = src["name"] 
        print(f"  Fetching {name}…") 
        if src["type"] == "boarddocs": 
            results[name] = fetch_boarddocs(src["url"], name) 
        elif src["type"] == "novusagenda": 
            results[name] = fetch_novusagenda(src["url"], name) 
        elif src["type"] == "boardbook": 
            results[name] = fetch_boardbook(src["url"], name) 
        elif src["type"] == "laserfiche": 
            results[name] = fetch_laserfiche_cs(name) 
        else: 
            results[name] = fetch_html_generic(src["url"], name) 
    return results 
 
 
# ───────────────────────────────────────────────────────────── 
# STATE & FEDERAL RSS FEEDS 
# ───────────────────────────────────────────────────────────── 
 
def _parse_rss(url: str, label: str, max_items: int = 8) -> str: 
    try: 
        r = requests.get(url, headers=HEADERS, timeout=20) 
        r.raise_for_status() 
        root = ET.fromstring(r.content) 
        ns   = {"atom": "http://www.w3.org/2005/Atom"} 
        items = root.findall(".//item") or root.findall(".//atom:entry", 
ns) 
        results = [] 
        for item in items[:max_items]: 
            title = ( 
                item.findtext("title") 
                or item.findtext("atom:title", namespaces=ns) or "" 
            ).strip() 
            desc = ( 
                item.findtext("description") 
                or item.findtext("summary") 
                or item.findtext("atom:summary", namespaces=ns) or "" 
            ) 
            desc  = re.sub(r"<[^>]+>", " ", desc) 
            desc  = re.sub(r"\s+", " ", desc).strip()[:150] 
            link  = ( 
                item.findtext("link") 
                or item.findtext("atom:link", namespaces=ns) or "" 
            ).strip() 
            pub   = ( 
                item.findtext("pubDate") 
                or item.findtext("published") 
                or item.findtext("atom:published", namespaces=ns) or "" 
            ).strip() 
            if title: 
                results.append(f"[{pub}] {title}\n{desc}\n{link}") 
        return ( 
            f"=== {label} ===\n" + "\n\n".join(results) 
            if results else f"=== {label} ===\n(no recent items)" 
        ) 
    except Exception as e: 
        return f"=== {label} ===\n[RSS error: {e}]" 
 
 
def _scrape_page_text(url: str, label: str, max_chars: int = 2500) -> 
str: 
    try: 
        r = requests.get(url, headers=HEADERS, timeout=20) 
        r.raise_for_status() 
        soup = BeautifulSoup(r.text, "html.parser") 
        for tag in soup(["script","style","nav","footer","header"]): 
            tag.decompose() 
        text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True)) 
        return f"=== {label} ===\n{text[:max_chars]}" 
    except Exception as e: 
        return f"=== {label} ===\n[Scrape error: {e}]" 
 
 
def fetch_state_federal_sources() -> str: 
    print("  Fetching state/federal RSS feeds and agency pages…") 
    sections = [] 
 
    # max_items=5 and desc[:200] keeps each feed compact 
    sections.append(_parse_rss( 
        "https://www.tceq.texas.gov/news/rss", 
        "TCEQ News & Enforcement", max_items=5)) 
    sections.append(_scrape_page_text( 
        "https://www14.tceq.texas.gov/epic/CIO/index.cfm" 
        "?fuseaction=search.search&county=021", 
        "TCEQ Enforcement – Brazos County", max_chars=1000)) 
    sections.append(_parse_rss( 
        "https://gov.texas.gov/news/rss", 
        "Texas Governor Press Releases", max_items=5)) 
    sections.append(_parse_rss( 
        "https://www.texastribune.org/feeds/latest/", 
        "Texas Tribune – Latest", max_items=5)) 
    sections.append(_parse_rss( 
        "https://www.texastribune.org/feeds/education/", 
        "Texas Tribune – Education", max_items=4)) 
    sections.append(_parse_rss( 
        "https://www.federalregister.gov/api/v1/documents.rss" 
        "?conditions[agencies][]=department-of-agriculture" 
        "&conditions[states][]=TX", 
        "Federal Register – USDA / Texas", max_items=4)) 
    sections.append(_parse_rss( 
        "https://www.federalregister.gov/api/v1/documents.rss" 
        "?conditions[agencies][]=environmental-protection-agency" 
        "&conditions[states][]=TX", 
        "Federal Register – EPA / Texas", max_items=4)) 
    sections.append(_parse_rss( 
        "https://www.federalregister.gov/api/v1/documents.rss" 
        "?conditions[agencies][]=department-of-housing-and-urban
development" 
        "&conditions[states][]=TX", 
        "Federal Register – HUD / Texas", max_items=4)) 
    sections.append(_scrape_page_text( 
        "https://www.fsa.usda.gov/state-offices/Texas/news-and
events/index", 
        "USDA FSA – Texas News", max_chars=1000)) 
    sections.append(_parse_rss( 
        "https://tea.texas.gov/about-tea/news-and-multimedia/news
releases", 
        "Texas Education Agency News", max_items=4)) 
    sections.append(_scrape_page_text( 
        "https://www.tdcj.texas.gov/divisions/cmhc/news.html", 
        "TDCJ News", max_chars=800)) 
    sections.append(_scrape_page_text( 
        "https://www.txdot.gov/about/newsroom/statewide.html", 
        "TxDOT Statewide News", max_chars=800)) 
 
    # Hard cap on total state/federal content to keep prompt under 11k 
tokens 
    combined = "\n\n".join(sections) 
    return combined[:18000] 
 
 
# ───────────────────────────────────────────────────────────── 
# JSON PARSING HELPER  (shared by both AI providers) 
# ───────────────────────────────────────────────────────────── 
 
def _parse_pitches_from_text(raw: str) -> list[dict] | None: 
    """ 
    Try to extract a valid list of pitch dicts from raw model output. 
    Returns the list on success, or None if parsing fails. 
    """ 
    text = raw.strip() 
    text = re.sub(r"^```(?:json)?\s*", "", text) 
    text = re.sub(r"\s*```$",          "", text) 
 
    # Try direct parse 
    try: 
        result = json.loads(text) 
        if isinstance(result, list) and result: 
            return result 
    except json.JSONDecodeError: 
        pass 
 
    # Try to find a JSON array anywhere in the response 
    match = re.search(r"\[.*\]", text, re.DOTALL) 
    if match: 
        try: 
            result = json.loads(match.group()) 
            if isinstance(result, list) and result: 
                return result 
        except json.JSONDecodeError: 
            pass 
 
    return None 
 
 
def _error_pitch(message: str) -> list[dict]: 
    return [{ 
        "headline":  "Brief generation encountered an error", 
        "angle":     message[:400], 
        "source":    "System log", 
        "next_step": "Check GitHub Actions run log for details.", 
        "urgency":   "TODAY", 
        "category":  "SYSTEM", 
    }] 
 
 
# ───────────────────────────────────────────────────────────── 
# AI PROVIDER 1 — GROQ  (primary) 
# ───────────────────────────────────────────────────────────── 
 
def _try_groq(prompt: str, attempt: int = 1) -> list[dict] | None: 
    """ 
    Call Groq API. Returns parsed pitches or None on any failure. 
    Retries once on bad JSON before giving up. 
    """ 
    if not GROQ_AVAILABLE or not GROQ_API_KEY: 
        print("  [Groq] Skipped — library not installed or key missing.") 
        return None 
 
    try: 
        client = Groq(api_key=GROQ_API_KEY) 
        print(f"  [Groq] Calling Llama 3.3 70B (attempt {attempt})…") 
 
        response = client.chat.completions.create( 
            model="llama-3.3-70b-versatile", 
            messages=[{"role": "user", "content": prompt}], 
            temperature=0.3, 
            max_tokens=4096, 
        ) 
 
        raw     = response.choices[0].message.content 
        pitches = _parse_pitches_from_text(raw) 
 
        if pitches: 
            print(f"  [Groq] ✓ {len(pitches)} pitches parsed.") 
            return pitches 
 
        # Bad JSON on first attempt — retry once with a stricter nudge 
        if attempt == 1: 
            print("  [Groq] JSON parse failed — retrying with stricter 
prompt…") 
            time.sleep(2) 
            stricter = prompt + ( 
                "\n\nCRITICAL: Your previous response could not be parsed 
as JSON. " 
                "Return ONLY the raw JSON array. No text before [. No 
text after ]." 
            ) 
            return _try_groq(stricter, attempt=2) 
 
        print(f"  [Groq] Could not parse JSON after {attempt} attempts.") 
        return None 
 
    except Exception as e: 
        print(f"  [Groq] Error: {e}") 
        return None 
 
 
# ───────────────────────────────────────────────────────────── 
# AI PROVIDER 2 — GEMINI  (fallback) 
# ───────────────────────────────────────────────────────────── 
 
def _try_gemini(prompt: str) -> list[dict] | None: 
    """ 
    Call Gemini API using the new google-genai package. 
    Returns parsed pitches or None on any failure. 
    """ 
    if not GEMINI_AVAILABLE or not GEMINI_API_KEY: 
        print("  [Gemini] Skipped — library not installed or key 
missing.") 
        return None 
 
    try: 
        client = google_genai.Client(api_key=GEMINI_API_KEY) 
        print("  [Gemini] Calling Gemini 2.0 Flash (fallback)…") 
 
        response = client.models.generate_content( 
            model="gemini-2.0-flash", 
            contents=prompt, 
        ) 
        pitches = _parse_pitches_from_text(response.text) 
 
        if pitches: 
            print(f"  [Gemini] ✓ {len(pitches)} pitches parsed.") 
            return pitches 
 
        print("  [Gemini] Could not parse JSON from response.") 
        return None 
 
    except Exception as e: 
        print(f"  [Gemini] Error: {e}") 
        return None 
 
 
# ───────────────────────────────────────────────────────────── 
# PITCH GENERATION — ORCHESTRATOR 
# ───────────────────────────────────────────────────────────── 
 
def generate_pitches(agenda_data: dict, state_federal_data: str) -> 
list[dict]: 
    """ 
    Build the prompt, try Groq first, fall back to Gemini, 
    and return error pitches only if both fail. 
    """ 
    agenda_block = "".join( 
        f"\n\n=== {org.upper()} ===\n{content}" 
        for org, content in agenda_data.items() 
    ) 
 
    prompt = PITCH_PROMPT_TEMPLATE.format( 
        today             = TODAY, 
        outlets           = ", ".join(LOCAL_OUTLETS), 
        counties          = ", ".join(COVERAGE_COUNTIES), 
        agenda_block      = agenda_block, 
        state_federal_data= state_federal_data, 
    ) 
 
    # ── 1. Try Groq ─────────────────────────────────────────── 
    pitches = _try_groq(prompt) 
    if pitches: 
        return pitches 
 
    # ── 2. Fall back to Gemini ──────────────────────────────── 
    print("  Groq unavailable or failed — switching to Gemini fallback…") 
    pitches = _try_gemini(prompt) 
    if pitches: 
        return pitches 
 
    # ── 3. Both failed ──────────────────────────────────────── 
    print("    Both AI providers failed. Delivering error brief.") 
    return _error_pitch( 
        "Both Groq and Gemini failed to return valid pitches. " 
        "Check GitHub Actions log for details." 
    ) 
 
 
# ───────────────────────────────────────────────────────────── 
# DELIVERY: EMAIL 
# ───────────────────────────────────────────────────────────── 
 
def _pitch_card_html(i: int, p: dict) -> str: 
    color = URGENCY_COLOR.get(p.get("urgency", "TODAY"), "#333") 
    emoji = CATEGORY_EMOJI.get(p.get("category", ""), " ") 
    return f""" 
    <div style="background:#fff;border:1px solid #dde; 
                border-left:5px solid {color};border-radius:8px; 
                padding:20px;margin-bottom:18px;"> 
      <div style="margin-bottom:8px;"> 
        <span style="background:{color};color:#fff;font-size:11px;font
weight:700; 
                     padding:3px 9px;border-radius:4px;margin-right:8px; 
                     text
transform:uppercase;">{p.get("urgency","")}</span> 
        <span style="color:#888;font-size:12px;">{emoji} 
{p.get("category","")}</span> 
      </div> 
      <h2 style="margin:0 0 10px;font-size:17px;color:#111;"> 
        {i}. {p.get("headline","")} 
      </h2> 
      <p style="margin:0 0 12px;color:#444;line-height:1.65;font
size:.95em;"> 
        {p.get("angle","")} 
      </p> 
      <table style="width:100%;border-collapse:collapse;font
size:13px;color:#555;"> 
        <tr> 
          <td style="padding:3px 10px 3px 0;white-space:nowrap;vertical
align:top; 
                     font-weight:600;color:#888;">SOURCE</td> 
          <td style="padding:3px 0;">{p.get("source","")}</td> 
        </tr> 
        <tr> 
          <td style="padding:3px 10px 3px 0;white-space:nowrap;vertical
align:top; 
                     font-weight:600;color:#888;">NEXT STEP</td> 
          <td style="padding:3px 0;">{p.get("next_step","")}</td> 
        </tr> 
      </table> 
    </div>""" 
 
 
def build_email_html(pitches: list[dict], webpage_url: str) -> str: 
    cards = "".join(_pitch_card_html(i, p) for i, p in enumerate(pitches, 
1)) 
    return f"""<!DOCTYPE html> 
<html><head><meta charset="UTF-8"></head> 
<body style="margin:0;padding:0;background:#f0f0f4; 
             font-family:-apple-system,BlinkMacSystemFont,'Segoe 
UI',sans-serif;"> 
  <div style="max-width:680px;margin:0 auto;padding:20px;"> 
    <div style="background:#1a1a2e;border-radius:8px 8px 0 0;padding:24px 
28px;"> 
      <h1 style="margin:0;color:#fff;font-size:1.4rem;"> 
         Brazos Valley Morning Brief 
      </h1> 
      <p style="margin:6px 0 0;color:#99a;font-size:.85rem;">{TODAY}</p> 
    </div> 
    <div style="background:#e4e4ec;padding:10px 28px;margin-bottom:22px; 
                border-radius:0 0 8px 8px;font-size:12px;color:#666;"> 
      {len(pitches)} original pitches &nbsp;•&nbsp; 
      Sources: government agendas + state/federal RSS &nbsp;•&nbsp; 
      <a href="{webpage_url}" style="color:#555;">View webpage</a> 
    </div> 
    {cards} 
    <p style="text-align:center;color:#aaa;font-size:11px;margin
top:28px;"> 
      Brazos Valley Morning Brief &nbsp;•&nbsp; Auto-generated · $0/month 
    </p> 
  </div> 
</body></html>""" 
 
 
def send_email(pitches: list[dict], webpage_url: str): 
    recipients = [RECIPIENT_EMAIL] 
    if EXTRA_EMAILS: 
        recipients += [e.strip() for e in EXTRA_EMAILS.split(",") if 
e.strip()] 
 
    msg            = MIMEMultipart("alternative") 
    msg["Subject"] = f" Morning Brief — {len(pitches)} pitches — 
{TODAY}" 
    msg["From"]    = GMAIL_ADDRESS 
    msg["To"]      = ", ".join(recipients) 
 
    plain = f"BRAZOS VALLEY MORNING BRIEF\n{TODAY}\n{'─'*44}\n\n" 
    for i, p in enumerate(pitches, 1): 
        plain += ( 
            f"{i}. [{p.get('urgency','')}] {p.get('headline','')}\n" 
            f"   {p.get('angle','')}\n" 
            f"   SOURCE: {p.get('source','')}\n" 
            f"   NEXT STEP: {p.get('next_step','')}\n\n" 
        ) 
    plain += f"\nFull brief: {webpage_url}" 
 
    msg.attach(MIMEText(plain, "plain")) 
    msg.attach(MIMEText(build_email_html(pitches, webpage_url), "html")) 
 
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server: 
        server.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD) 
        server.sendmail(GMAIL_ADDRESS, recipients, msg.as_string()) 
 
    print(f"  ✓ Email sent to {', '.join(recipients)}") 
 
 
# ───────────────────────────────────────────────────────────── 
# DELIVERY: GITHUB PAGES WEBPAGE 
# ───────────────────────────────────────────────────────────── 
 
def write_webpage(pitches: list[dict]) -> str: 
    cards = "" 
    for i, p in enumerate(pitches, 1): 
        color = URGENCY_COLOR.get(p.get("urgency", "TODAY"), "#333") 
        emoji = CATEGORY_EMOJI.get(p.get("category", ""), " ") 
        cards += f""" 
      <div class="card" style="--accent:{color}"> 
        <div class="meta"> 
          <span class="badge">{p.get("urgency","")}</span> 
          <span class="cat">{emoji} {p.get("category","")}</span> 
        </div> 
        <h2>{i}. {p.get("headline","")}</h2> 
        <p class="angle">{p.get("angle","")}</p> 
        <dl class="details"> 
          <dt>Source</dt><dd>{p.get("source","")}</dd> 
          <dt>Next Step</dt><dd>{p.get("next_step","")}</dd> 
        </dl> 
      </div>""" 
 
    html = f"""<!DOCTYPE html> 
<html lang="en"> 
<head> 
  <meta charset="UTF-8"> 
  <meta name="viewport" content="width=device-width,initial-scale=1"> 
  <title>Brazos Valley Morning Brief — {TODAY}</title> 
  <style> 
    *,*::before,*::after{{box-sizing:border-box}} 
    body{{margin:0;background:#f2f2f6; 
         font-family:-apple-system,BlinkMacSystemFont,'Segoe 
UI',Roboto,sans-serif; 
         color:#1a1a1a}} 
    header{{background:#1a1a2e;color:#fff;padding:28px 24px}} 
    header h1{{margin:0;font-size:1.55rem;font-weight:700}} 
    header p{{margin:6px 0 0;color:#99a;font-size:.85rem}} 
    .wrap{{max-width:780px;margin:0 auto;padding:24px 16px 60px}} 
    .summary{{background:#e4e4ec;border-radius:8px;padding:12px 16px; 
              font-size:.83rem;color:#555;margin-bottom:24px;line
height:1.6}} 
    .card{{background:#fff;border:1px solid #dde; 
           border-left:5px solid var(--accent);border-radius:8px; 
           padding:22px;margin-bottom:20px; 
           box-shadow:0 1px 4px rgba(0,0,0,.06)}} 
    .meta{{display:flex;align-items:center;gap:10px;margin-bottom:10px}} 
    .badge{{background:var(--accent);color:#fff;font-size:11px;font
weight:700; 
            padding:3px 9px;border-radius:4px; 
            text-transform:uppercase;letter-spacing:.05em}} 
    .cat{{font-size:12px;color:#888}} 
    h2{{margin:0 0 10px;font-size:1.05rem;line-height:1.45;font
weight:600}} 
    .angle{{margin:0 0 14px;color:#444;line-height:1.65;font
size:.95rem}} 
    dl.details{{margin:0;font-size:.83rem;color:#555;line-height:1.7}} 
    dt{{font-weight:600;color:#888;float:left;margin-right:6px}} 
    dd{{margin:0 0 4px}} 
    footer{{text-align:center;padding:30px 16px;color:#aaa;font
size:.75rem}} 
    @media(max-width:500px){{h2{{font-size:.97rem}}}} 
  </style> 
</head> 
<body> 
<header> 
  <h1> Brazos Valley Morning Brief</h1> 
  <p>{TODAY} &nbsp;•&nbsp; {len(pitches)} original story pitches</p> 
</header> 
<div class="wrap"> 
  <div class="summary"> 
    <strong>Agendas checked:</strong> 
    Brazos County Commissioners Court • City of Bryan • City of College 
Station • 
    Bryan ISD • College Station ISD • Texas A&M System Regents • Blinn 
College<br> 
    <strong>State/federal sources:</strong> 
    TCEQ • Texas Governor • Texas Tribune • Federal Register 
(USDA/EPA/HUD) • 
    TEA • TDCJ • TxDOT<br> 
    <strong>Filtered out:</strong> Stories already covered by KBTX, The 
Eagle, and WTAW. 
  </div> 
  {cards} 
</div> 
<footer> 
  Generated automatically each weekday morning · 
  GitHub Actions + Groq (Llama 3.3 70B) / Gemini 1.5 Flash fallback · 
$0/month 
</footer> 
</body> 
</html>""" 
 
    os.makedirs("docs", exist_ok=True) 
    with open("docs/index.html", "w", encoding="utf-8") as f: 
        f.write(html) 
    print("  ✓ docs/index.html written.") 
 
    repo     = os.environ.get("GITHUB_REPOSITORY", 
"YOUR_USERNAME/morning-brief") 
    username = repo.split("/")[0] 
    reponame = repo.split("/")[1] if "/" in repo else "morning-brief" 
    return f"https://{username}.github.io/{reponame}/" 
 
 
# ───────────────────────────────────────────────────────────── 
# MAIN 
# ───────────────────────────────────────────────────────────── 
 
def main(): 
    print(f"\n{'═'*56}") 
    print(f"  BRAZOS VALLEY MORNING BRIEF — {TODAY}") 
    print(f"  Groq (primary) → Gemini (fallback) · $0/month") 
    print(f"{'═'*56}\n") 
 
    print("STEP 1 — Fetching government agendas…") 
    agenda_data = fetch_all_agendas() 
 
    print("\nSTEP 2 — Fetching state/federal RSS feeds…") 
    state_federal_data = fetch_state_federal_sources() 
 
    print("\nSTEP 3 — Generating pitches…") 
    pitches = generate_pitches(agenda_data, state_federal_data) 
    print(f"          {len(pitches)} pitches ready.") 
 
    print("\nSTEP 4 — Delivering briefing…") 
    webpage_url = write_webpage(pitches) 
    send_email(pitches, webpage_url) 
 
    print(f"\n✓ Done. Brief delivered for {TODAY}.\n") 
 
 
if __name__ == "__main__": 
    main() 
 
