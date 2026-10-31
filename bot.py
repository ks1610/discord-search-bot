import os
import re
import math
from collections import Counter
import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv
from serp_search import (SearchSession, PROFILES, fmt_price, fmt_vnd, price_input_info, strip_accents)

load_dotenv()
TOKEN = os.getenv('DISCORD_TOKEN')

intents = discord.Intents.default()
intents.message_content = True

PAGE_SIZE = 5
MAX_COMPARE = 4
LABELS = ['A', 'B', 'C', 'D']
SORT_LABELS = {
    'relevance': 'Liên quan nhất',
    'price_asc': 'Giá thấp → cao',
    'price_desc': 'Giá cao → thấp',
    'rating_desc': 'Đánh giá cao → thấp',
    'area_desc': 'Diện tích lớn → nhỏ',
}


class SearchBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix='!', intents=intents)

    async def setup_hook(self):
        guild_id = os.getenv('DISCORD_GUILD_ID')
        if guild_id:
            guild = discord.Object(id=int(guild_id))
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            print(f"Đã đồng bộ {len(synced)} lệnh vào server {guild_id}")
        else:
            synced = await self.tree.sync()
            print(f"Đã đồng bộ toàn cầu {len(synced)} lệnh")


bot = SearchBot()


def safe_link(url: str) -> str:
    return url.replace('(', '%28').replace(')', '%29')


def _words(text: str):
    return [w.strip() for w in (text or '').split(',') if w.strip()]


def _num(text: str):
    try:
        return float((text or '').strip().replace(',', '.'))
    except ValueError:
        return None


def _g(x):
    return '' if x is None else f"{x:g}"


class FilterModal(discord.ui.Modal):
    def __init__(self, v: 'SearchView'):
        super().__init__(title="Bộ lọc nâng cao")
        self.v = v
        self.must = discord.ui.TextInput(
            label="Phải chứa (phân tách bằng dấu phẩy)", required=False, max_length=200,
            default=', '.join(v.must), placeholder="vd: nội thất, gần hồ")
        self.exclude = discord.ui.TextInput(
            label="Loại trừ (phân tách bằng dấu phẩy)", required=False, max_length=200,
            default=', '.join(v.exclude), placeholder="vd: facebook, tin cũ")
        self.add_item(self.must)
        self.add_item(self.exclude)
        self.pmin = self.pmax = None
        if v.price_cur:
            unit = v.price_unit
            self.pmin = discord.ui.TextInput(label=f"Giá tối thiểu ({unit})"[:45], required=False,
                                             max_length=15, default=_g(v.min_price))
            self.pmax = discord.ui.TextInput(label=f"Giá tối đa ({unit})"[:45], required=False,
                                             max_length=15, default=_g(v.max_price))
            self.add_item(self.pmin)
            self.add_item(self.pmax)

    async def on_submit(self, interaction: discord.Interaction):
        self.v.must = _words(self.must.value)
        self.v.exclude = _words(self.exclude.value)
        if self.pmin is not None:
            self.v.min_price = _num(self.pmin.value)
            self.v.max_price = _num(self.pmax.value)
        self.v.page = 0
        await self.v.refresh(interaction)


class SearchView(discord.ui.View):
    def __init__(self, session: SearchSession, owner_id: int):
        super().__init__(timeout=600)
        self.session = session
        self.owner_id = owner_id
        self.message = None
        self.all = session.results
        self.by_id = {r['id']: r for r in self.all}

        self.page = 0
        self.total_pages = 1
        self.filtered = list(self.all)
        self.domains = set()
        self.must, self.exclude = [], []
        self.min_price = self.max_price = None
        self.sort = 'relevance'
        self.selected = []
        self.note = ''
        self.price_cur, self.price_scale, self.price_unit = None, 1, ''

        self.domain_select = discord.ui.Select(
            placeholder="🌐 Lọc theo nguồn (mặc định: tất cả)", min_values=0, max_values=1, row=0,
            options=[discord.SelectOption(label="—", value="-")])
        self.domain_select.callback = self.on_domain
        self.sort_select = discord.ui.Select(
            placeholder="↕️ Sắp xếp", row=1, options=[discord.SelectOption(label="—", value="relevance")])
        self.sort_select.callback = self.on_sort
        self.compare_select = discord.ui.Select(
            placeholder=f"⚖️ Chọn kết quả để so sánh (tối đa {MAX_COMPARE})", min_values=0, max_values=1, row=2,
            options=[discord.SelectOption(label="—", value="-1")])
        self.compare_select.callback = self.on_pick
        for item in (self.domain_select, self.sort_select, self.compare_select):
            self.add_item(item)

        self.recompute_meta()
        self.apply()
        self.sync_components()

    def recompute_meta(self):
        cur = Counter(r['currency'] for r in self.all if r['price_min'] is not None).most_common(1)
        self.price_cur = cur[0][0] if cur else None
        self.price_scale, self.price_unit = price_input_info(self.price_cur)

    def available_sorts(self):
        s = ['relevance']
        if self.price_cur:
            s += ['price_asc', 'price_desc']
        if any(r['rating'] for r in self.all):
            s.append('rating_desc')
        if any(r['area'] for r in self.all):
            s.append('area_desc')
        return s

    def apply(self):
        res = list(self.all)
        if self.domains:
            res = [r for r in res if r['source'] in self.domains]
        if self.must or self.exclude:
            def hay(r):
                return strip_accents(f"{r['title']} {r['snippet']} {r['source']}").lower()
            must = [strip_accents(w).lower() for w in self.must]
            exc = [strip_accents(w).lower() for w in self.exclude]
            res = [r for r in res if all(w in hay(r) for w in must) and not any(w in hay(r) for w in exc)]
        if self.price_cur and (self.min_price is not None or self.max_price is not None):
            lo = None if self.min_price is None else self.min_price * self.price_scale
            hi = None if self.max_price is None else self.max_price * self.price_scale
            res = [r for r in res if r['currency'] == self.price_cur and r['price_min'] is not None
                   and (lo is None or r['price_max'] >= lo) and (hi is None or r['price_min'] <= hi)]

        if self.sort not in self.available_sorts():
            self.sort = 'relevance'
        cur = self.price_cur
        if self.sort == 'price_asc':
            res.sort(key=lambda r: (r['currency'] != cur or r['price_min'] is None, r['price_min'] or 0))
        elif self.sort == 'price_desc':
            res.sort(key=lambda r: (r['currency'] != cur or r['price_max'] is None, -(r['price_max'] or 0)))
        elif self.sort == 'rating_desc':
            res.sort(key=lambda r: (r['rating'] is None, -(r['rating'] or 0)))
        elif self.sort == 'area_desc':
            res.sort(key=lambda r: (r['area'] is None, -(r['area'] or 0)))
        else:
            res.sort(key=lambda r: r['id'])

        self.filtered = res
        self.total_pages = max(1, math.ceil(len(res) / PAGE_SIZE))
        self.page = max(0, min(self.page, self.total_pages - 1))

    def page_items(self):
        s = self.page * PAGE_SIZE
        return self.filtered[s:s + PAGE_SIZE]

    def sync_components(self):
        self.prev_button.disabled = self.page <= 0
        self.next_button.disabled = self.page >= self.total_pages - 1
        self.compare_button.label = f"So sánh ({len(self.selected)})"
        self.compare_button.disabled = len(self.selected) < 2
        self.more_button.disabled = not self.session.can_more

        counts = Counter(r['source'] for r in self.all)
        top = [s for s, _ in counts.most_common(25)]
        for s in self.domains:
            if s not in top and len(top) < 25:
                top.append(s)
        self.domain_select.max_values = max(1, len(top))
        self.domain_select.options = [
            discord.SelectOption(label=s[:100] or '?', value=s, description=f"{counts[s]} kết quả",
                                 default=s in self.domains) for s in top] or [discord.SelectOption(label="—", value="-")]

        self.sort_select.options = [
            discord.SelectOption(label=SORT_LABELS[k], value=k, default=(k == self.sort))
            for k in self.available_sorts()]

        items = self.page_items()
        if items:
            self.compare_select.disabled = False
            self.compare_select.max_values = len(items)
            self.compare_select.options = [
                discord.SelectOption(
                    label=f"{r['id'] + 1}. {r['title']}"[:100], value=str(r['id']),
                    description=f"{r['source']} · {fmt_price(r['price_min'], r['price_max'], r['currency'])}"[:100]
                    if r['price_min'] is not None else r['source'][:100],
                    default=r['id'] in self.selected) for r in items]
        else:
            self.compare_select.disabled = True
            self.compare_select.max_values = 1
            self.compare_select.options = [discord.SelectOption(label="—", value="-1")]

    def _meta(self, r):
        parts = []
        if r['price_min'] is not None:
            parts.append(f"💰 {fmt_price(r['price_min'], r['price_max'], r['currency'])}")
        if r['rating']:
            rv = f" ({r['reviews']})" if r['reviews'] else ''
            parts.append(f"⭐ {r['rating']:g}{rv}")
        if r['date']:
            parts.append(f"🗓️ {r['date']}")
        if r['area']:
            parts.append(f"📐 {r['area']:g} m²")
        parts.append(f"🌐 {r['source']}")
        return ' · '.join(parts)

    def build_embed(self):
        p = self.session.profile
        embed = discord.Embed(title=f"{p.emoji} {self.session.query}"[:256], color=discord.Color.blue())

        mode = f"Chế độ: {p.emoji} {p.label}" + (" (tự động)" if self.session.detected and not self.session.fallback else "")
        if self.session.fallback:
            mode += " · không có kết quả chuyên biệt nên đã chuyển sang Web"
        lines = [mode]
        active = []
        if self.domains:
            active.append("Nguồn: " + ", ".join(sorted(self.domains)))
        if self.must:
            active.append("Chứa: " + ", ".join(self.must))
        if self.exclude:
            active.append("Loại: " + ", ".join(self.exclude))
        if self.min_price is not None or self.max_price is not None:
            active.append(f"Giá: {_g(self.min_price) or '…'}–{_g(self.max_price) or '…'} {self.price_unit} (ẩn bài chưa rõ giá)")
        if self.sort != 'relevance':
            active.append("Sắp xếp: " + SORT_LABELS[self.sort])
        if active:
            lines.append("🧰 " + " · ".join(active))
        if not self.page_items():
            lines.append("\nKhông có kết quả nào khớp bộ lọc. Bấm **Đặt lại** để xóa bộ lọc.")
        embed.description = "\n".join(lines)

        for n, r in enumerate(self.page_items(), start=self.page * PAGE_SIZE + 1):
            snippet = r['snippet'] if len(r['snippet']) <= 180 else r['snippet'][:177] + "..."
            extras = ' · '.join(f"{k}: {v}" for k, v in r['extras'][:3])
            mark = "✅ " if r['id'] in self.selected else ""
            body = [snippet, self._meta(r)]
            if extras:
                body.append(extras[:200])
            body.append(f"🔗 [Mở liên kết]({safe_link(r['link'])})")
            embed.add_field(name=f"{n}. {mark}{r['title']}"[:256], value="\n".join(body)[:1024], inline=False)

        foot = f"Trang {self.page + 1}/{self.total_pages} | Hiển thị {len(self.filtered)}/{len(self.all)} kết quả"
        if self.note:
            foot += f" | {self.note}"
        embed.set_footer(text=foot[:2048])
        return embed

    def build_compare_embed(self):
        items = [self.by_id[i] for i in self.selected]
        embed = discord.Embed(title="⚖️ So sánh kết quả", color=discord.Color.gold())

        cur = Counter(r['currency'] for r in items if r['price_min'] is not None).most_common(1)
        cur = cur[0][0] if cur else None
        has_rating = any(r['rating'] for r in items)
        has_date = any(r['date'] for r in items)
        has_area = any(r['area'] for r in items)
        has_phone = any(r['phone'] for r in items)
        extra_labels = []
        for r in items:
            for k, _ in r['extras']:
                if k not in extra_labels:
                    extra_labels.append(k)

        notes = []
        priced = [(i, r) for i, r in enumerate(items) if r['currency'] == cur and r['price_min'] is not None]
        if len(priced) >= 2:
            i, r = min(priced, key=lambda t: t[1]['price_min'])
            notes.append(f"💰 Giá thấp nhất: **{LABELS[i]}** ({fmt_price(r['price_min'], r['price_max'], cur)})")
        rated = [(i, r) for i, r in enumerate(items) if r['rating']]
        if len(rated) >= 2:
            i, r = max(rated, key=lambda t: t[1]['rating'])
            notes.append(f"⭐ Đánh giá cao nhất: **{LABELS[i]}** ({r['rating']:g})")
        sized = [(i, r) for i, r in enumerate(items) if r['area']]
        if len(sized) >= 2:
            i, r = max(sized, key=lambda t: t[1]['area'])
            notes.append(f"📐 Rộng nhất: **{LABELS[i]}** ({r['area']:g} m²)")
        notes.append("⚠️ Thông tin được đọc tự động từ đoạn mô tả, hãy mở link để xác nhận.")
        embed.description = "\n".join(notes)

        for i, r in enumerate(items):
            lines = [f"🌐 {r['source']}"]
            if cur:
                lines.append(f"💰 {fmt_price(r['price_min'], r['price_max'], r['currency'])}")
            if has_rating:
                lines.append(f"⭐ {r['rating']:g}" + (f" ({r['reviews']})" if r['reviews'] else '') if r['rating'] else "⭐ Chưa rõ")
            if has_date:
                lines.append(f"🗓️ {r['date']}" if r['date'] else "🗓️ Chưa rõ")
            if has_area:
                lines.append(f"📐 {r['area']:g} m²" if r['area'] else "📐 Chưa rõ")
                if r['area'] and r['price_min'] is not None and r['price_min'] == r['price_max'] and r['currency'] == 'VND':
                    lines.append(f"≈ {fmt_vnd(r['price_min'] / r['area'])}/m²")
            if has_phone:
                lines.append(f"📞 {r['phone']}" if r['phone'] else "📞 Chưa rõ")
            ex = dict(r['extras'])
            for k in extra_labels:
                lines.append(f"{k}: {ex[k][:80]}" if k in ex else f"{k}: —")
            lines.append(f"🔗 [Mở liên kết]({safe_link(r['link'])})")
            embed.add_field(name=f"{LABELS[i]}. {r['title']}"[:256], value="\n".join(lines)[:1024], inline=True)
        return embed

    async def refresh(self, interaction: discord.Interaction):
        self.apply()
        self.sync_components()
        embed = self.build_embed()
        self.note = ''
        await interaction.response.edit_message(embed=embed, view=self)

    async def on_domain(self, interaction: discord.Interaction):
        self.domains = {v for v in self.domain_select.values if v != '-'}
        self.page = 0
        await self.refresh(interaction)

    async def on_sort(self, interaction: discord.Interaction):
        self.sort = self.sort_select.values[0]
        self.page = 0
        await self.refresh(interaction)

    async def on_pick(self, interaction: discord.Interaction):
        page_ids = [r['id'] for r in self.page_items()]
        chosen = {int(v) for v in self.compare_select.values if int(v) >= 0}
        kept = [i for i in self.selected if i not in page_ids]
        self.selected = (kept + [i for i in page_ids if i in chosen])[:MAX_COMPARE]
        await self.refresh(interaction)

    @discord.ui.button(label="◀", style=discord.ButtonStyle.primary, row=3)
    async def prev_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page -= 1
        await self.refresh(interaction)

    @discord.ui.button(label="▶", style=discord.ButtonStyle.primary, row=3)
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page += 1
        await self.refresh(interaction)

    @discord.ui.button(label="So sánh (0)", style=discord.ButtonStyle.success, disabled=True, row=3)
    async def compare_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(embed=self.build_compare_embed(), ephemeral=True)

    @discord.ui.button(label="🔧 Lọc nâng cao", style=discord.ButtonStyle.secondary, row=4)
    async def filter_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(FilterModal(self))

    @discord.ui.button(label="🔄 Đặt lại", style=discord.ButtonStyle.danger, row=4)
    async def reset_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.domains, self.must, self.exclude = set(), [], []
        self.min_price = self.max_price = None
        self.sort, self.selected, self.page = 'relevance', [], 0
        await self.refresh(interaction)

    @discord.ui.button(label="➕ Tải thêm", style=discord.ButtonStyle.secondary, row=4)
    async def more_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        added = 0
        try:
            added = await self.session.more()
        except Exception as e:
            print(f"Lỗi khi tải thêm: {e}")
        self.all = self.session.results
        self.by_id = {r['id']: r for r in self.all}
        self.recompute_meta()
        self.note = f"Đã thêm {added} kết quả mới" if added else "Không có thêm kết quả mới"
        self.apply()
        self.sync_components()
        embed = self.build_embed()
        self.note = ''
        await interaction.edit_original_response(embed=embed, view=self)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("Chỉ người dùng lệnh mới bấm được nút này.", ephemeral=True)
            return False
        return True

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


@bot.event
async def on_ready():
    print(f'Logged in as {bot.user.name} (ID: {bot.user.id})')
    print('Bot đã sẵn sàng hoạt động!')


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    # Danh sách tên/từ khóa tên channel áp dụng tìm kiếm trực tiếp không cần @
    SEARCH_CHANNEL_KEYWORDS = ['fu-channel']
    
    channel_name = getattr(message.channel, 'name', '').lower()
    is_search_channel = any(kw in channel_name for kw in SEARCH_CHANNEL_KEYWORDS)
    is_mentioned = (bot.user in message.mentions)

    if is_search_channel or is_mentioned:
        query = re.sub(rf'<@!?{bot.user.id}>', '', message.content).strip()

        if not query:
            return

        async with message.channel.typing():
            session = SearchSession(
                query,
                mode='auto',
                lang='auto',
                limit=30,
                deep=False,
                sites=[],
                time='any',
            )
            await session.run()

            if not session.results:
                await message.reply(f"Không tìm thấy kết quả nào cho từ khóa: **{query}**")
                return

            view = SearchView(session, owner_id=message.author.id)
            view.message = await message.reply(embed=view.build_embed(), view=view)

    await bot.process_commands(message)

# @bot.event
# async def on_message(message: discord.Message):
#     # Bỏ qua tin nhắn từ bot khác hoặc từ chính nó
#     if message.author.bot:
#         return

#     if bot.user in message.mentions:
#         query = re.sub(rf'<@!?{bot.user.id}>', '', message.content).strip()

#         if not query:
#             await message.reply("Bạn hãy nhập từ khóa tìm kiếm sau khi tag bot nhé! Ví dụ: `@fu trọ vinhomes ocean park`")
#             return

#         async with message.channel.typing():
#             session = SearchSession(
#                 query,
#                 mode='auto',
#                 lang='auto',
#                 limit=30,
#                 deep=False,
#                 sites=[],
#                 time='any',
#             )
#             await session.run()

#             if not session.results:
#                 await message.reply(f"Không tìm thấy kết quả nào cho từ khóa: **{query}**")
#                 return

#             view = SearchView(session, owner_id=message.author.id)
#             view.message = await message.reply(embed=view.build_embed(), view=view)

#     await bot.process_commands(message)


if __name__ == '__main__':
    bot.run(TOKEN)