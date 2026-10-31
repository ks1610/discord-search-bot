"""
Flexible search layer.

A *Profile* describes one kind of search (web, news, shopping, jobs, scholar, video, places,
real estate...): which SerpAPI engine to call, where the results live in the response, and how
to turn one raw item into the common result format used by the bot.

Adding a new field = write one normalize function + one Profile(...) entry in PROFILES.
"""
import os
import re
import math
import asyncio
import unicodedata
from dataclasses import dataclass
from typing import Callable, Optional
from urllib.parse import urlparse, parse_qs, parse_qsl, unquote, urlencode, urlunparse
from serpapi import GoogleSearch
from dotenv import load_dotenv

load_dotenv()
SERPAPI_KEY = os.getenv('SERPAPI_KEY')

TRACKING_PARAM = re.compile(r'^(utm_|gclid|fbclid|gad_|msclkid|mc_|ref$|ref_)', re.I)
ALWAYS_BLOCKED = ('googleadservices.com', 'doubleclick.net')
MAX_PER_DOMAIN = 3     # soft cap, only while other results are still available
MAX_PAGES = 10
ALIASES = {r'\bvinhome\b': 'vinhomes'}   # common typos -> canonical word (edit freely)

# ------------------------------------------------------------------ text helpers
VN_CHARS = re.compile(
    r'[àáảãạăằắẳẵặâầấẩẫậèéẻẽẹêềếểễệìíỉĩịòóỏõọôồốổỗộơờớởỡợùúủũụưừứửữựỳýỷỹỵđ]', re.I)
VN_KEYWORDS = re.compile(
    r'\b(tro|nha tro|nha|phong tro|vinhomes|ocean ?park|ha noi|hanoi|sai gon|tp ?hcm|'
    r'gia|trieu|phong|can ho|chung cu|thue|mua ban|o dau|bao nhieu|la gi|tin tuc|viec lam)\b')
FILLER = re.compile(
    r'^\s*(?:tìm kiếm|tim kiem|tìm|tim|search)\b\s*(?:giúp|giup|cho)?\s*(?:mình|minh|tôi|toi)?\s*', re.I)


def _fetch_serp_data(params):
    return GoogleSearch(params).get_dict()


def strip_accents(text: str) -> str:
    text = text.replace('đ', 'd').replace('Đ', 'D')
    return ''.join(c for c in unicodedata.normalize('NFD', text) if unicodedata.category(c) != 'Mn')


def is_vietnamese(text: str) -> bool:
    return bool(VN_CHARS.search(text)) or bool(VN_KEYWORDS.search(strip_accents(text).lower()))


def clean_query(query: str) -> str:
    q = FILLER.sub('', query.strip())
    for pat, rep in ALIASES.items():
        q = re.sub(pat, rep, q, flags=re.I)
    q = re.sub(r'\s+', ' ', q).strip()
    return q or query.strip()


def _tokens(text: str) -> set:
    return {t for t in re.findall(r'\w+', strip_accents(text).lower()) if len(t) > 1}


def _relevance(q_tokens: set, title: str, snippet: str, rank: int) -> float:
    base = 1 / (1 + rank * 0.15)
    if not q_tokens:
        return base
    return (base + 0.6 * len(q_tokens & _tokens(title)) / len(q_tokens)
            + 0.3 * len(q_tokens & _tokens(snippet)) / len(q_tokens))


def _s(v) -> str:
    if isinstance(v, dict):
        v = v.get('name') or v.get('title') or ''
    return '' if v is None else str(v).strip()


# ------------------------------------------------------------------ URL cleaning
def clean_url(link: str, allow_google: bool = False) -> str:
    link = (link or '').strip()
    if not link:
        return ''
    parsed = urlparse(link)
    host = (parsed.hostname or '').lower()
    if host.endswith('google.com') and parsed.path in ('/url', '/aclk'):
        qs = parse_qs(parsed.query)
        parsed = urlparse(unquote((qs.get('q') or qs.get('url') or [''])[0]))
        host = (parsed.hostname or '').lower()
    if parsed.scheme not in ('http', 'https') or '.' not in host:
        return ''
    if any(host == h or host.endswith('.' + h) for h in ALWAYS_BLOCKED):
        return ''
    if not allow_google and (host == 'google.com' or host.endswith('.google.com')):
        return ''
    query = urlencode([(k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True)
                       if not TRACKING_PARAM.match(k)])
    return urlunparse(parsed._replace(query=query, fragment=''))


# ------------------------------------------------------------------ price / area / phone
VND_SUFFIX = re.compile(r'(?<![\d.,])(\d{1,3}(?:[.,]\d{3}){1,2})\s*(?:đ|vnđ|vnd|đồng|₫)(?![a-zà-ỹ])', re.I)
VND_BARE = re.compile(r'(?<![\d.,])(\d{1,3}(?:[.,]\d{3}){2})(?!\d)')
MILLION = re.compile(r'(\d+(?:[.,]\d+)?(?:\s*[-–~]\s*\d+(?:[.,]\d+)?)*)\s*(?:triệu|trieu|tr)\b(?!\s*(?:lượt|luot|người|nguoi|views?|subs?))', re.I)
AREA = re.compile(r'(\d{2,3}(?:[.,]\d+)?)\s*(?:m2|m²|m\^2|㎡|mét vuông)', re.I)
PHONE = re.compile(r'(?<!\d)(0\d{2,3}[\s.-]?\d{3}[\s.-]?\d{3,4})(?!\d)')


def extract_prices(text: str):
    """-> (min, max, currency). VND values are returned in đồng; currency is 'VND', '$', '€' or '£'."""
    vnd = []

    def full(m):
        vnd.append(float(re.sub(r'[.,]', '', m.group(1))))
        return ' '
    t = VND_SUFFIX.sub(full, text)
    t = VND_BARE.sub(full, t)
    for m in MILLION.finditer(t):
        for n in re.findall(r'\d+(?:[.,]\d+)?', m.group(1)):
            vnd.append(float(n.replace(',', '.')) * 1_000_000)
    vnd = [v for v in vnd if 5_000 <= v <= 50_000_000_000]
    if vnd:
        return min(vnd), max(vnd), 'VND'

    for sym, code in (('$', 'usd'), ('€', 'eur'), ('£', 'gbp')):
        rx = re.compile(
            rf'(?:{re.escape(sym)}|{code})\s?(\d[\d,]*(?:\.\d+)?)|(\d[\d,]*(?:\.\d+)?)\s?(?:{code}\b|{re.escape(sym)}(?!\s?\d))', re.I)
        vals = []
        for m in rx.finditer(t):
            try:
                vals.append(float((m.group(1) or m.group(2)).rstrip(',').replace(',', '')))
            except ValueError:
                pass
        vals = [v for v in vals if 0.01 <= v <= 1e7]
        if vals:
            return min(vals), max(vals), sym
    return None, None, None


def detect_currency(price_text: str) -> Optional[str]:
    p = price_text.lower()
    if '₫' in p or 'đ' in p or 'vnd' in p:
        return 'VND'
    if '$' in p or 'usd' in p:
        return '$'
    if '€' in p or 'eur' in p:
        return '€'
    if '£' in p or 'gbp' in p:
        return '£'
    return None


def fmt_vnd(v: float) -> str:
    return f"{v / 1e6:g} triệu" if v >= 1_000_000 else f"{v:,.0f}đ".replace(',', '.')


def fmt_price(pmin, pmax, cur) -> str:
    if pmin is None:
        return "Chưa rõ"
    if cur == 'VND':
        if pmin == pmax:
            return fmt_vnd(pmin)
        if pmin >= 1e6 and pmax >= 1e6:
            return f"{pmin / 1e6:g}–{pmax / 1e6:g} triệu"
        return f"{fmt_vnd(pmin)} – {fmt_vnd(pmax)}"

    def f(v):
        return f"{cur}{v:,.0f}" if float(v).is_integer() else f"{cur}{v:,.2f}"
    return f(pmin) if pmin == pmax else f"{f(pmin)} – {f(pmax)}"


def price_input_info(cur: Optional[str]):
    """(scale, label) used by the filter modal: VND is typed in millions."""
    return (1_000_000, 'triệu VND') if cur == 'VND' else (1, cur or '')


# ------------------------------------------------------------------ normalizers (raw item -> common fields)
def _norm_web(it):
    return dict(title=_s(it.get('title')), link=_s(it.get('link')),
                snippet=_s(it.get('snippet')), date=_s(it.get('date')) or None)


def _norm_news(it):
    return dict(title=_s(it.get('title')), link=_s(it.get('link')), snippet=_s(it.get('snippet')),
                source=_s(it.get('source')), date=_s(it.get('date')) or None)


def _norm_shopping(it):
    exts = ', '.join(map(str, it.get('extensions') or []))
    snippet = ' · '.join(x for x in (_s(it.get('snippet')), _s(it.get('delivery')), exts) if x)
    return dict(title=_s(it.get('title')), link=_s(it.get('link') or it.get('product_link')),
                snippet=snippet, source=_s(it.get('source')), price_text=_s(it.get('price')),
                price_value=it.get('extracted_price'), rating=it.get('rating'), reviews=it.get('reviews'))


def _norm_job(it):
    ext = it.get('detected_extensions') or {}
    opts = it.get('apply_options') or []
    link = (opts[0].get('link') if opts else '') or it.get('share_link') or ''
    return dict(title=_s(it.get('title')), link=_s(link), snippet=_s(it.get('description'))[:300],
                source=_s(it.get('via')).removeprefix('via ').strip(), date=_s(ext.get('posted_at')) or None,
                price_text=_s(ext.get('salary')),
                extras=[('Công ty', _s(it.get('company_name'))), ('Địa điểm', _s(it.get('location'))),
                        ('Hình thức', _s(ext.get('schedule_type'))), ('Lương', _s(ext.get('salary')))])


def _norm_scholar(it):
    summary = _s((it.get('publication_info') or {}).get('summary'))
    cited = ((it.get('inline_links') or {}).get('cited_by') or {}).get('total')
    year = re.search(r'\b(?:19|20)\d{2}\b', summary)
    return dict(title=_s(it.get('title')), link=_s(it.get('link')), snippet=_s(it.get('snippet')),
                date=year.group(0) if year else None,
                extras=[('Tác giả / nguồn', summary), ('Trích dẫn', '' if cited is None else str(cited))])


def _norm_video(it):
    ch, views = _s(it.get('channel')), it.get('views')
    views_s = f"{views:,}" if isinstance(views, int) else _s(views)
    extras = [('Kênh', ch), ('Lượt xem', views_s), ('Thời lượng', _s(it.get('length')))]
    return dict(title=_s(it.get('title')), link=_s(it.get('link')),
                snippet=_s(it.get('description')) or ' · '.join(v for _, v in extras if v),
                source=ch, date=_s(it.get('published_date')) or None, extras=extras)


def _norm_place(it):
    pid, addr = it.get('place_id'), _s(it.get('address'))
    link = _s(it.get('website')) or (f"https://www.google.com/maps/place/?q=place_id:{pid}" if pid else '')
    kind = _s(it.get('type'))
    return dict(title=_s(it.get('title')), link=link, snippet=' · '.join(x for x in (addr, kind) if x),
                rating=it.get('rating'), reviews=it.get('reviews'), phone=_s(it.get('phone')),
                extras=[('Địa chỉ', addr), ('Loại', kind)])


# ------------------------------------------------------------------ profiles
@dataclass
class Profile:
    key: str
    label: str
    emoji: str
    engine: str
    keys: tuple
    normalize: Callable
    tbm: Optional[str] = None
    paginate: bool = False          # supports "start" pagination -> "load more" works
    cap: bool = False               # soft per-domain cap
    prices: bool = False            # try to read prices out of title/snippet
    area: bool = False
    allow_google: bool = False      # result links legitimately live on google.com
    default_sites: tuple = ()       # extra site: queries in deep mode
    detect: Optional[re.Pattern] = None      # tested on accent-stripped lowercase query
    detect_raw: Optional[re.Pattern] = None  # tested on lowercase query with accents


RENTAL_SOURCES = ('batdongsan.com.vn', 'chotot.com', 'phongtro123.com', 'alonhadat.com.vn')

PROFILES = {p.key: p for p in [
    Profile('realestate', 'Bất động sản / thuê nhà', '🏠', 'google', ('organic_results',), _norm_web,
            paginate=True, cap=True, prices=True, area=True, default_sites=RENTAL_SOURCES,
            detect=re.compile(r'\b(tro|nha tro|phong tro|thue (nha|phong|can ho|mat bang)|can ho|chung cu|nha dat|'
                              r'ban nha|cho thue|bat dong san|studio|vinhomes)\b')),
    Profile('jobs', 'Việc làm', '💼', 'google_jobs', ('jobs_results',), _norm_job, prices=True,
            detect=re.compile(r'\b(viec lam|tuyen dung|tim viec|jobs?|hiring|career)\b')),
    Profile('scholar', 'Học thuật / nghiên cứu', '🎓', 'google_scholar', ('organic_results',), _norm_scholar,
            paginate=True,
            detect=re.compile(r'\b(papers?|nghien cuu|research|arxiv|doi|journal|luan van|thesis|scholar|citation)\b')),
    Profile('video', 'Video', '🎬', 'youtube', ('video_results',), _norm_video,
            detect=re.compile(r'\b(video|youtube|clip|trailer|livestream)\b')),
    Profile('news', 'Tin tức', '📰', 'google', ('news_results', 'organic_results'), _norm_news,
            tbm='nws', paginate=True, cap=True,
            detect=re.compile(r'\b(tin tuc|news|moi nhat|hom nay|thoi su|breaking|latest)\b')),
    Profile('places', 'Địa điểm / dịch vụ', '📍', 'google_maps', ('local_results',), _norm_place,
            allow_google=True,
            detect=re.compile(r'\b(quan an|nha hang|ca phe|gan day|gan toi|khach san|near me|restaurant|cafe|hotel)\b'),
            detect_raw=re.compile(r'(quán|nhà hàng|cà phê|gần đây|khách sạn|phòng khám|bệnh viện|\bspa\b)')),
    Profile('shopping', 'Mua sắm / so sánh giá', '🛒', 'google_shopping', ('shopping_results',), _norm_shopping,
            prices=True, allow_google=True,
            detect=re.compile(r'\b(mua|gia|price|buy|cheap|re nhat|khuyen mai|sale|deal|laptop|dien thoai|iphone|'
                              r'samsung|tai nghe|tivi|tu lanh|may giat)\b')),
    Profile('web', 'Web tổng hợp', '🌐', 'google', ('organic_results',), _norm_web,
            paginate=True, cap=True, prices=True),
]}


def detect_profile(cleaned: str) -> Profile:
    plain, raw = strip_accents(cleaned).lower(), cleaned.lower()
    for p in PROFILES.values():
        if (p.detect and p.detect.search(plain)) or (p.detect_raw and p.detect_raw.search(raw)):
            return p
    return PROFILES['web']


def find_items(data: dict, profile: Profile) -> list:
    for k in profile.keys:
        v = data.get(k)
        if isinstance(v, list) and v:
            return v
    if profile.key not in ('web', 'realestate'):      # generic fallback for engines whose key differs
        for v in data.values():
            if isinstance(v, list) and v and isinstance(v[0], dict) and 'title' in v[0]:
                return v
    return []


def build_params(profile: Profile, q: str, lang: str, page: int, time: str, lang_hint: str) -> dict:
    p = {"engine": profile.engine, "api_key": SERPAPI_KEY}
    p["search_query" if profile.engine == "youtube" else "q"] = q
    if profile.tbm:
        p["tbm"] = profile.tbm
    if profile.engine == "google_maps":
        p["type"] = "search"
    if profile.paginate:
        p["start"] = page * 10

    vi = lang == "vi" or (lang == "auto" and is_vietnamese(lang_hint))
    if vi:
        p.update({"hl": "vi", "gl": "vn"})
        if profile.engine in ("google", "google_shopping", "google_jobs"):
            p["google_domain"] = "google.com.vn"
        if profile.engine in ("google", "google_scholar"):
            p["lr"] = "lang_vi"
    elif lang == "en":
        p.update({"hl": "en", "gl": "us"})
        if profile.engine in ("google", "google_shopping", "google_jobs"):
            p["google_domain"] = "google.com"
        if profile.engine in ("google", "google_scholar"):
            p["lr"] = "lang_en"

    if time in ("d", "w", "m", "y") and profile.engine == "google":
        p["tbs"] = f"qdr:{time}"
    return p


# ------------------------------------------------------------------ session
class SearchSession:
    def __init__(self, query, mode='auto', lang='auto', limit=30, deep=False, sites=None, time='any'):
        self.query, self.mode, self.lang, self.limit = query, mode, lang, limit
        self.deep, self.time = deep, time
        self.sites = [s for s in (sites or []) if s]
        self.cleaned = clean_query(query)
        self.q_tokens = _tokens(self.cleaned)
        self.profile: Profile = PROFILES['web']
        self.detected = False
        self.fallback = False
        self.results: list = []
        self.seen: set = set()
        self.next_page = 1
        self.exhausted = False

    @property
    def can_more(self) -> bool:
        return self.profile.paginate and not self.exhausted and self.next_page < MAX_PAGES

    async def _run_variant(self, profile, q, start_page, pages) -> list:
        out = []
        try:
            for i in range(pages):
                params = build_params(profile, q, self.lang, start_page + i, self.time, self.cleaned)
                data = await asyncio.to_thread(_fetch_serp_data, params)
                if data.get('error'):
                    print(f"SerpAPI [{profile.engine}] {data['error']}")
                items = find_items(data, profile)
                if not items:
                    break
                out.extend(items)
                if not profile.paginate:
                    break
        except Exception as e:
            print(f"Lỗi khi gọi SerpAPI ({profile.engine}, {q!r}): {e}")
        return out

    def _entry(self, profile: Profile, raw: dict, rank: int):
        try:
            n = profile.normalize(raw)
        except Exception:
            return None
        link = clean_url(n.get('link', ''), allow_google=profile.allow_google)
        if not link:
            return None
        key = link.rstrip('/').lower()
        if key in self.seen:
            return None
        self.seen.add(key)

        title = n.get('title') or 'Không có tiêu đề'
        snippet = n.get('snippet') or 'Không có mô tả.'
        domain = (urlparse(link).hostname or '').removeprefix('www.')
        price_text = n.get('price_text') or ''

        pmin = pmax = cur = None
        pv = n.get('price_value')
        if isinstance(pv, (int, float)) and pv > 0 and detect_currency(price_text):
            cur = detect_currency(price_text)
            pmin = pmax = float(pv)
        elif profile.prices or price_text:
            pmin, pmax, cur = extract_prices(f"{price_text}. {title}. {snippet}")

        rating = n.get('rating')
        text = f"{title}. {snippet}"
        area = None
        if profile.area:
            areas = [float(a.replace(',', '.')) for a in AREA.findall(text)]
            areas = [a for a in areas if 8 <= a <= 500]
            area = areas[0] if areas else None
        phone = _s(n.get('phone'))
        if not phone:
            m = PHONE.search(text)
            digits = re.sub(r'\D', '', m.group(1)) if m else ''
            phone = digits if len(digits) == 10 else ''

        return {
            'title': title, 'link': link, 'snippet': snippet, 'domain': domain,
            'source': (_s(n.get('source')) or domain)[:100],
            'score': _relevance(self.q_tokens, title, snippet, rank),
            'price_min': pmin, 'price_max': pmax, 'currency': cur,
            'date': n.get('date'),
            'rating': float(rating) if isinstance(rating, (int, float)) else None,
            'reviews': n.get('reviews'),
            'area': area, 'phone': phone or None,
            'extras': [(k, str(v)[:120]) for k, v in (n.get('extras') or []) if v],
        }

    def _entries(self, profile, raw_items) -> list:
        out = []
        for rank, raw in enumerate(raw_items):
            e = self._entry(profile, raw, rank)
            if e:
                out.append(e)
        return out

    async def _initial(self, profile: Profile) -> list:
        pages = 1
        if profile.paginate:
            pages = 3 if self.deep else min(3, max(1, math.ceil(self.limit / 10)))
        self.next_page = pages

        variants = [(self.cleaned, pages)]
        if profile.engine == 'google':
            sites = list(self.sites) + (list(profile.default_sites) if self.deep else [])
            variants += [(f"{self.cleaned} site:{s}", 1) for s in dict.fromkeys(sites)]

        raw_lists = await asyncio.gather(*[self._run_variant(profile, q, 0, p) for q, p in variants])
        cands = []
        for raw in raw_lists:
            cands.extend(self._entries(profile, raw))
        return cands

    def _finalize(self, cands: list) -> list:
        cands.sort(key=lambda r: r['score'], reverse=True)
        if self.profile.cap:
            per, picked, skipped = {}, [], []
            for r in cands:
                if per.get(r['domain'], 0) >= MAX_PER_DOMAIN:
                    skipped.append(r)
                    continue
                per[r['domain']] = per.get(r['domain'], 0) + 1
                picked.append(r)
            cands = picked + skipped
        cands = cands[:self.limit]
        for i, r in enumerate(cands):
            r['id'] = i
        return cands

    async def run(self):
        if self.mode in PROFILES:
            self.profile = PROFILES[self.mode]
        else:
            self.profile, self.detected = detect_profile(self.cleaned), True

        cands = await self._initial(self.profile)
        if not cands and self.profile.key != 'web':          # specialised engine found nothing -> plain web
            self.fallback = True
            self.profile = PROFILES['web']
            self.seen.clear()
            cands = await self._initial(self.profile)
        self.results = self._finalize(cands)
        return self

    async def more(self) -> int:
        """Fetch one more page of the main query. Returns how many new results were added."""
        raw = await self._run_variant(self.profile, self.cleaned, self.next_page, 1)
        self.next_page += 1
        if not raw:
            self.exhausted = True
            return 0
        cands = self._entries(self.profile, raw)
        cands.sort(key=lambda r: r['score'], reverse=True)
        for c in cands:
            c['id'] = len(self.results)
            self.results.append(c)
        return len(cands)