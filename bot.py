# bot.py  --- 完全版（セレクト＋ボタンUI付き・修正適用）
# 要件:
# - 1メッセージに複数グループ（バンドル）を表示し、定期更新・ピン留め・リンク抑止
# - /task_groups で一覧、/task_groups_edit で編集（バンドル/グループ）
# - 入力簡略化（ショートカット＆補完＆プリセット）＋ モーダル入力
# - /task_groups_ui で「セレクト＋ボタンUI」による対話編集（ラベル編集、リネーム、削除、即時更新、pin/suppress切替、interval変更、グループ追加）
# 依存: pip install "discord.py>=2.3" PyGithub aiosqlite python-dateutil
# 環境変数:
#   DISCORD_TOKEN, DISCORD_GUILD_ID(任意), GITHUB_TOKEN, GITHUB_OWNER, GITHUB_REPO
# 重要:
#   - discord.py 2.3+想定。リンク抑止は msg.edit(suppress=True) を使用。古い場合は suppress_embeds(True) にフォールバック。
#   - CommandTree.clear_commands は同期関数。await を付けない。

import os
import json
import asyncio
from typing import List, Optional, Tuple, Dict, Callable, TypeVar, Union
from datetime import datetime, date, timezone, timedelta

import aiosqlite
import discord
from discord import app_commands
from discord.ext import tasks
from github import Github, GithubException
from github.Issue import Issue as GH_Issue

# ========= 環境変数 =========
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = int(os.getenv("DISCORD_GUILD_ID", "0")) or None  # None ならグローバル
GH_TOKEN = os.getenv("GITHUB_TOKEN")
GH_OWNER = os.getenv("GITHUB_OWNER")
GH_REPO = os.getenv("GITHUB_REPO")

# ========= 定数 =========
DB_PATH = "bot.db"
STATUS_LABELS = {"status:todo", "status:in_progress", "status:done"}
DEFAULT_INTERVAL_MIN = int(os.getenv("LIST_UPDATE_INTERVAL_MIN", "5"))  # 既定 5分
MAX_PER_SECTION = 50
DISCORD_MSG_LIMIT = 2000
TASK_LIST_PAGE_SIZE = 6
TASK_LIST_EMBED_COLOR = 0x2B90D9

# ========= Issueテンプレ =========
ISSUE_TEMPLATES: Dict[str, Dict] = {
    "bug": {
        "title_prefix": "[Bug] ",
        "body": "### 再現\n1. \n2. \n3.\n\n### 期待\n\n### 実際\n\n### 環境\n- OS:\n- Build:\n",
        "labels": ["type:bug", "status:todo"],
    },
    "task": {
        "title_prefix": "[Task] ",
        "body": "### 概要\n\n### 完了条件\n- [ ] \n- [ ] \n",
        "labels": ["type:task", "status:todo"],
    },
    "feature": {
        "title_prefix": "[Feature] ",
        "body": "### 提案\n\n### 目的\n\n### 受入条件\n- [ ] \n- [ ] \n",
        "labels": ["type:feature", "status:todo"],
    },
}

# ========= ユーティリティ =========
# --- 時刻ユーティリティ（JST表記）
JST = timezone(timedelta(hours=9))

def now_jst_str() -> str:
    return datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S JST")
    
def gh_client() -> Github:
    if not GH_TOKEN:
        raise RuntimeError("GITHUB_TOKEN 未設定")
    return Github(GH_TOKEN, per_page=100)

# --- DB: 旧binding互換 + 新: bundle/bundle_group ---
async def db_init():
    async with aiosqlite.connect(DB_PATH) as db:
        # DEFAULT <整数> を直接埋め込む
        await db.execute(f"""
        CREATE TABLE IF NOT EXISTS binding (
          id INTEGER PRIMARY KEY CHECK (id=1),
          channel_id INTEGER NOT NULL,
          list_message_id INTEGER NOT NULL,
          label_filters TEXT DEFAULT '[]',
          interval_min INTEGER DEFAULT {int(DEFAULT_INTERVAL_MIN)}
        )""")

        await db.execute("""
        CREATE TABLE IF NOT EXISTS user_link (
          discord_user_id INTEGER PRIMARY KEY,
          github_login TEXT NOT NULL
        )""")

        await db.execute("""
        CREATE TABLE IF NOT EXISTS preset (
          name TEXT PRIMARY KEY,
          label_filters TEXT NOT NULL,
          interval_min INTEGER NOT NULL
        )""")

        await db.execute("""
        CREATE TABLE IF NOT EXISTS bundle (
          channel_id INTEGER PRIMARY KEY,
          message_id INTEGER NOT NULL,
          interval_min INTEGER NOT NULL,
          pin INTEGER NOT NULL DEFAULT 1,
          suppress INTEGER NOT NULL DEFAULT 1
        )""")

        await db.execute("""
        CREATE TABLE IF NOT EXISTS bundle_group (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          channel_id INTEGER NOT NULL,
          group_name TEXT NOT NULL,
          label_filters TEXT NOT NULL,
          UNIQUE(channel_id, group_name)
        )""")

        # 旧 binding_group（存在しなくてもOK）
        await db.execute("""
        CREATE TABLE IF NOT EXISTS binding_group (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          channel_id INTEGER NOT NULL,
          message_id INTEGER NOT NULL,
          group_name TEXT NOT NULL,
          label_filters TEXT NOT NULL,
          interval_min INTEGER NOT NULL,
          pin INTEGER NOT NULL DEFAULT 1,
          suppress INTEGER NOT NULL DEFAULT 1
        )""")

        # 旧 binding -> bundle への移行
        cur = await db.execute(
            "SELECT channel_id, list_message_id, label_filters, COALESCE(interval_min, ?) FROM binding WHERE id=1",
            (int(DEFAULT_INTERVAL_MIN),)
        )
        old = await cur.fetchone()
        if old:
            ch, msg, labs_json, iv = old
            cur2 = await db.execute("SELECT 1 FROM bundle WHERE channel_id=?", (ch,))
            if not await cur2.fetchone():
                await db.execute(
                    "INSERT INTO bundle (channel_id, message_id, interval_min, pin, suppress) VALUES (?, ?, ?, 1, 1)",
                    (ch, msg, int(iv))
                )
            cur3 = await db.execute("SELECT 1 FROM bundle_group WHERE channel_id=? AND group_name='default'", (ch,))
            if not await cur3.fetchone():
                await db.execute(
                    "INSERT OR IGNORE INTO bundle_group (channel_id, group_name, label_filters) VALUES (?, 'default', ?)",
                    (ch, labs_json or "[]")
                )
            await db.execute("DELETE FROM binding WHERE id=1")

        # 旧 binding_group -> bundle へ寄せる（あれば）
        cur = await db.execute("SELECT channel_id, message_id, group_name, label_filters, interval_min, pin, suppress FROM binding_group")
        rows = await cur.fetchall()
        for ch, msg, name, labs_json, iv, pin, sup in rows:
            curb = await db.execute("SELECT 1 FROM bundle WHERE channel_id=?", (ch,))
            if not await curb.fetchone():
                await db.execute(
                    "INSERT INTO bundle (channel_id, message_id, interval_min, pin, suppress) VALUES (?, ?, ?, ?, ?)",
                    (ch, msg, int(iv), int(pin), int(sup))
                )
            await db.execute(
                "INSERT OR IGNORE INTO bundle_group (channel_id, group_name, label_filters) VALUES (?, ?, ?)",
                (ch, name, labs_json or "[]")
            )

        await db.commit()

async def preset_save(name: str, label_filters: List[str], interval_min: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO preset (name, label_filters, interval_min) VALUES (?, ?, ?) "
            "ON CONFLICT(name) DO UPDATE SET label_filters=excluded.label_filters, interval_min=excluded.interval_min",
            (name, json.dumps(label_filters), int(interval_min))
        )
        await db.commit()

async def preset_load(name: str) -> Optional[Tuple[List[str], int]]:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT label_filters, interval_min FROM preset WHERE name=?", (name,))
        row = await cur.fetchone()
        if not row:
            return None
        labels = json.loads(row[0]) if row[0] else []
        return (labels, int(row[1]))

async def preset_list(prefix: str = "") -> List[str]:
    async with aiosqlite.connect(DB_PATH) as db:
        if prefix:
            cur = await db.execute("SELECT name FROM preset WHERE name LIKE ? ORDER BY name LIMIT 25", (f"{prefix}%",))
        else:
            cur = await db.execute("SELECT name FROM preset ORDER BY name LIMIT 25")
        return [r[0] for r in await cur.fetchall()]

async def upsert_bundle(channel_id: int, message_id: int, interval_min: int, pin: bool, suppress: bool):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT 1 FROM bundle WHERE channel_id=?", (channel_id,))
        if await cur.fetchone():
            await db.execute("UPDATE bundle SET message_id=?, interval_min=?, pin=?, suppress=? WHERE channel_id=?",
                             (message_id, int(interval_min), 1 if pin else 0, 1 if suppress else 0, channel_id))
        else:
            await db.execute("INSERT INTO bundle (channel_id, message_id, interval_min, pin, suppress) VALUES (?, ?, ?, ?, ?)",
                             (channel_id, message_id, int(interval_min), 1 if pin else 0, 1 if suppress else 0))
        await db.commit()

async def get_bundle(channel_id: int) -> Optional[Tuple[int,int,int,bool,bool]]:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT channel_id, message_id, interval_min, pin, suppress FROM bundle WHERE channel_id=?", (channel_id,))
        row = await cur.fetchone()
        if not row:
            return None
        return (int(row[0]), int(row[1]), int(row[2]), bool(row[3]), bool(row[4]))

async def upsert_bundle_group(channel_id: int, group_name: str, label_filters: List[str]):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO bundle_group (channel_id, group_name, label_filters) VALUES (?, ?, ?) "
            "ON CONFLICT(channel_id, group_name) DO UPDATE SET label_filters=excluded.label_filters",
            (channel_id, group_name, json.dumps(label_filters))
        )
        await db.commit()

async def delete_bundle_group(channel_id: int, group_name: str) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("DELETE FROM bundle_group WHERE channel_id=? AND group_name=?", (channel_id, group_name))
        await db.commit()
        return cur.rowcount > 0

async def list_bundle_groups(channel_id: int) -> List[Tuple[str, List[str]]]:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT group_name, label_filters FROM bundle_group WHERE channel_id=? ORDER BY group_name", (channel_id,))
        out = []
        for name, labs in await cur.fetchall():
            out.append((name, json.loads(labs) if labs else []))
        return out

# ========= Due 抽出/強調 =========
def parse_due(i: GH_Issue) -> Optional[date]:
    if i.body:
        for line in i.body.splitlines():
            if line.strip().lower().startswith("due:"):
                s = line.split(":", 1)[1].strip()
                try:
                    return datetime.strptime(s, "%Y-%m-%d").date()
                except Exception:
                    pass
    for lab in i.labels:
        n = lab.name.strip()
        if n.lower().startswith("due:"):
            s = n.split(":", 1)[1].strip()
            try:
                return datetime.strptime(s, "%Y-%m-%d").date()
            except Exception:
                continue
    return None

def decorate_due_marker(i: GH_Issue) -> str:
    d = parse_due(i)
    if not d:
        return ''
    today = date.today()
    if d < today:
        return ' [期限超過]'
    if d == today:
        return ' [本日期限]'
    if (d - today).days <= 3:
        return ' [期限迫る]'
    return ''



def has_label(i: GH_Issue, name: str) -> bool:
    return any(l.name.lower() == name.lower() for l in i.labels)

def ensure_status_labels(labels: List[str]) -> List[str]:
    has_status = any(l.lower().startswith("status:") for l in labels)
    if not has_status:
        labels.append("status:todo")
    normalized = []
    for l in labels:
        if l.lower().startswith("status:") and l.lower() not in STATUS_LABELS:
            raise ValueError(f"不正な状態ラベル '{l}'. 許可: {', '.join(sorted(STATUS_LABELS))}")
        normalized.append(l)
    return normalized

# ========= GitHub: Issue作成（テンプレ/期日/ラベル） =========
async def gh_create_issue_with_template(
    title: str,
    body: Optional[str],
    assignee: Optional[str],
    labels_csv: Optional[str],
    due: Optional[str],
    template_key: Optional[str],
) -> GH_Issue:
    """
    - template_key: bug/task/feature のいずれか。None ならテンプレ未使用。
    - labels_csv: "a,b,c" 形式 or None
    - due: "YYYY-MM-DD" or None
    """
    def _work() -> GH_Issue:
        g = gh_client()
        repo = g.get_repo(f"{GH_OWNER}/{GH_REPO}")

        labels: List[str] = []
        if labels_csv:
            labels = [s.strip() for s in labels_csv.split(",") if s.strip()]
        if template_key and template_key in ISSUE_TEMPLATES:
            t = ISSUE_TEMPLATES[template_key]
            title_prefix = t.get("title_prefix", "")
            templ_body = t.get("body", "")
            templ_labels = t.get("labels", [])
            # type:* は重複しないようにマージ
            add_labels = [x for x in templ_labels if x not in labels]
            labels = labels + add_labels
            title_full = f"{title_prefix}{title}"
            if body and body.strip():
                body_full = f"{templ_body}\n\n---\n{body}"
            else:
                body_full = templ_body
        else:
            title_full = title
            body_full = body or ""

        # 期日
        if due:
            try:
                _ = datetime.strptime(due, "%Y-%m-%d")
                # Body に Due 行を付与、ラベルも付ける（parse_due は両対応）
                if "due:" not in body_full.lower():
                    body_full = f"{body_full}\n\nDue: {due}".strip()
                labels.append(f"due:{due}")
            except Exception:
                pass

        # ステータス保険
        labels = ensure_status_labels(labels)

        # 実際の作成（ラベルは存在しなくても作成時に付く。未定義でもOK）
        issue = repo.create_issue(
            title=title_full,
            body=body_full or None,
            assignee=assignee or None,
            labels=labels or None,
        )
        return issue

    return await asyncio.to_thread(_work)

# ========= Issue取得/描画 =========
def fetch_issues_sync(filters: List[str]) -> List[GH_Issue]:
    g = gh_client()
    repo = g.get_repo(f"{GH_OWNER}/{GH_REPO}")

    # 明示ループで安全に上限を切る（スライス禁止）
    issues: List[GH_Issue] = []

    for state in ["open", "closed"]:
        pl = repo.get_issues(state=state, sort="updated", direction="desc")
        fetched = 0
        for it in pl:
            issues.append(it)
            fetched += 1
            if fetched >= 200:
                break

    if filters:
        def match_filters(issue: GH_Issue) -> bool:
            names = {l.name for l in issue.labels}
            return all(f in names for f in filters)
        issues = [x for x in issues if match_filters(x)]

    return issues

def _shorten_title(title: str, limit: int = 70) -> str:
    return title if len(title) <= limit else title[: limit - 1] + '…'


def _format_updated_jst(dt: datetime) -> Tuple[str, str]:
    aware = dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    jst_dt = aware.astimezone(JST)
    delta = datetime.now(JST) - jst_dt
    if delta.total_seconds() < 0:
        rel = '未来'
    elif delta.days > 0:
        rel = f"{delta.days}日前"
    else:
        hours = delta.seconds // 3600
        if hours > 0:
            rel = f"{hours}時間前"
        else:
            minutes = (delta.seconds % 3600) // 60
            rel = f"{minutes}分前" if minutes > 0 else 'たった今'
    return (jst_dt.strftime('%Y-%m-%d %H:%M'), rel)


def render_issue_block(i: GH_Issue) -> str:
    title = _shorten_title(i.title)
    assignee = f"@{i.assignee.login}" if i.assignee else '未割当'
    due = parse_due(i)
    due_text = due.isoformat() if due else '未設定'
    mark = decorate_due_marker(i)
    updated_text, updated_rel = _format_updated_jst(i.updated_at)
    meta_parts = [
        f"担当:{assignee}",
        f"期限:{due_text}{mark}",
        f"更新:{updated_text}({updated_rel})",
    ]
    line1 = f"> `#{i.number}` {title}"
    line2 = f"> {' | '.join(meta_parts)}"
    line3 = f"> {i.html_url}"
    return '\n'.join([line1, line2, line3])

T = TypeVar('T')

def chunk_list(items: List[T], size: int) -> List[List[T]]:
    if size <= 0:
        raise ValueError("size must be positive")
    return [items[i:i + size] for i in range(0, len(items), size)]

def _status_from_issue(issue: GH_Issue) -> str:
    for label in issue.labels:
        name = label.name
        if name.lower().startswith("status:"):
            return name.split(":", 1)[1]
    return issue.state

def format_task_list_entry(issue: GH_Issue) -> str:
    title = _shorten_title(issue.title)
    mark = decorate_due_marker(issue)
    assignee = f"@{issue.assignee.login}" if issue.assignee else "未割当"
    due = parse_due(issue)
    due_text = due.isoformat() if due else "未設定"
    _, updated_rel = _format_updated_jst(issue.updated_at)
    status_text = _status_from_issue(issue)
    return f"[#{issue.number}]({issue.html_url}) {title}{mark} | 状態:{status_text} | 担当:{assignee} | 期限:{due_text} | 更新:{updated_rel}"

def build_task_list_embed(page_items: List[str], page_idx: int, page_total: int, title: str) -> discord.Embed:
    description = "\n".join(f"- {item}" for item in page_items) if page_items else "該当なし"
    embed = discord.Embed(title=title, description=description, color=TASK_LIST_EMBED_COLOR)
    embed.set_footer(text=f"Page {page_idx + 1}/{page_total}")
    return embed

class TaskListView(discord.ui.View):
    def __init__(self, bot: "Bot", entries: List[str], *, per_page: int = TASK_LIST_PAGE_SIZE, title: str = "タスク一覧（Embed）"):
        super().__init__(timeout=None)
        self.bot = bot
        self.entries = entries
        self.per_page = max(1, per_page)
        self.title = title
        self.pages = chunk_list(entries, self.per_page) or [[]]
        self.page_idx = 0

    @property
    def page_total(self) -> int:
        return len(self.pages)

    def current_embed(self) -> discord.Embed:
        return build_task_list_embed(self.pages[self.page_idx], self.page_idx, self.page_total, self.title)

    def _can_prev(self) -> bool:
        return self.page_idx > 0

    def _can_next(self) -> bool:
        return self.page_idx < self.page_total - 1

    @discord.ui.button(label="◀ 前", style=discord.ButtonStyle.secondary, row=1)
    async def btn_prev(self, interaction: discord.Interaction, _: discord.ui.Button):
        if not self._can_prev():
            await interaction.response.defer()
            return
        self.page_idx -= 1
        await interaction.response.edit_message(embed=self.current_embed(), view=self)

    @discord.ui.button(label="次 ▶", style=discord.ButtonStyle.secondary, row=1)
    async def btn_next(self, interaction: discord.Interaction, _: discord.ui.Button):
        if not self._can_next():
            await interaction.response.defer()
            return
        self.page_idx += 1
        await interaction.response.edit_message(embed=self.current_embed(), view=self)

    @discord.ui.button(label="🔄 再掲（末尾へ）", style=discord.ButtonStyle.primary, row=2)
    async def btn_repost_to_bottom(self, interaction: discord.Interaction, _: discord.ui.Button):
        channel = interaction.channel
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            await interaction.response.send_message("対応チャンネルでのみ利用できます。", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        new_msg = await self.bot._send_task_list_embed(channel, self.entries, self.title, page_idx=self.page_idx, per_page=self.per_page)
        await interaction.followup.send(f"最新を最下部に再掲しました: [jump]({new_msg.jump_url})", ephemeral=True)

async def run_issue_action(number: int, action: Callable[[GH_Issue], T]) -> T:
    def _work():
        repo = gh_client().get_repo(f"{GH_OWNER}/{GH_REPO}")
        issue = repo.get_issue(number)
        return action(issue)
    return await asyncio.to_thread(_work)


async def get_linked_login(discord_user_id: int) -> Optional[str]:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT github_login FROM user_link WHERE discord_user_id=?",
            (int(discord_user_id),)
        )
        row = await cur.fetchone()
        return row[0] if row else None


async def resolve_login_input(raw: Optional[str], discord_user_id: int) -> Optional[str]:
    if not raw:
        return None
    if raw.lower() != 'me':
        return raw
    return await get_linked_login(discord_user_id)


def replace_status_label(labels: List[str], new_status: Optional[str]) -> List[str]:
    seen = set()
    out: List[str] = []
    for name in labels:
        lower = name.lower()
        if lower.startswith('status:'):
            continue
        if lower not in seen:
            out.append(name)
            seen.add(lower)
    if new_status and new_status.lower() not in seen:
        out.append(new_status)
    return out


def remove_label(labels: List[str], target: str) -> List[str]:
    target_low = target.lower()
    return [l for l in labels if l.lower() != target_low]



async def build_group_section(title: str, filters: List[str]) -> str:
    issues = await asyncio.to_thread(lambda: fetch_issues_sync(filters))

    def overdue_rank(i: GH_Issue) -> int:
        d = parse_due(i)
        if not d:
            return 3
        today = date.today()
        if d < today:
            return 0
        if d == today:
            return 1
        if (d - today).days <= 3:
            return 2
        return 3

    doing = [i for i in issues if i.state == 'open' and has_label(i, 'status:in_progress')]
    todo = [i for i in issues if i.state == 'open' and has_label(i, 'status:todo')]

    doing.sort(key=lambda i: (overdue_rank(i), i.updated_at))
    todo.sort(key=lambda i: (overdue_rank(i), i.updated_at))

    doing = doing[:MAX_PER_SECTION]
    todo = todo[:MAX_PER_SECTION]

    def render_group(label: str, items: List[GH_Issue]) -> str:
        header = f"**{label}** ({len(items)}件)"
        if not items:
            return "\n".join([header, "> 該当なし"])
        blocks = "\n\n".join(render_issue_block(i) for i in items)
        return header + "\n" + blocks

    section_parts: List[str] = [f"__**{title}**__"]
    if filters:
        section_parts.append(f"`labels: {', '.join(filters)}`")
    section_parts.append(render_group("進行中 (in_progress)", doing))
    section_parts.append("")
    section_parts.append(render_group("未着手 (todo)", todo))
    section_parts.append("")

    q = f"repo:{GH_OWNER}/{GH_REPO} is:issue is:open"
    if filters:
        q += ''.join(f" label:{f}" for f in filters)
    more_url = f"https://github.com/{GH_OWNER}/{GH_REPO}/issues?q={q.replace(' ', '+')}"
    section_parts.append(f"一覧: {more_url}")
    section_parts.append(f"_section updated: {now_jst_str()}_")

    return "\n".join(section_parts).strip()




async def build_bundle_content(channel_id: int) -> str:
    groups = await list_bundle_groups(channel_id)
    if not groups:
        return "*（このチャンネルにはグループがありません。`/task_group_add` または `/task_group_add_modal` で追加してください）*"

    sections: List[str] = []
    for name, filters in groups:
        sections.append(await build_group_section(name, filters))

    content = "\n\n".join(sections)

    # ★ バンドル最終更新（JST）
    footer = f"\n\n— **最終更新**: {now_jst_str()}"
    content = content + footer

    if len(content) <= DISCORD_MSG_LIMIT:
        return content
    return content[: DISCORD_MSG_LIMIT - 20] + "\n…(省略)"

# ========= 入力簡略化（ラベル補完/ショートカット） =========
_LABEL_CACHE: Dict[str, List[str]] = {}
def _label_cache_key() -> str:
    return f"{GH_OWNER}/{GH_REPO}"

async def get_repo_labels_cached() -> List[str]:
    key = _label_cache_key()
    if key in _LABEL_CACHE:
        return _LABEL_CACHE[key]
    def _fetch():
        g = gh_client()
        repo = g.get_repo(f"{GH_OWNER}/{GH_REPO}")
        return [l.name for l in repo.get_labels()]
    labels = await asyncio.to_thread(_fetch)
    labels.sort(key=str.lower)
    _LABEL_CACHE[key] = labels
    return labels

LABEL_SHORTCUTS = {
    "todo": "status:todo",
    "doing": "status:in_progress",
    "in_progress": "status:in_progress",
    "done": "status:done",
    "#bug": "type:bug",
    "#task": "type:task",
    "#feature": "type:feature",
}

def normalize_label_input(raw: str) -> List[str]:
    if not raw:
        return []
    s = raw
    for sep in [",", ";"]:
        s = s.replace(sep, " ")
    tokens = []
    for t in s.split():
        t_low = t.strip().lower()
        if not t_low:
            continue
        tokens.append(LABEL_SHORTCUTS.get(t_low, t.strip()))
    seen = set()
    out = []
    for t in tokens:
        if t not in seen:
            out.append(t)
            seen.add(t)
    return out

async def autocomplete_labels(interaction: discord.Interaction, current: str):
    base = (current or "")
    for sep in [",", ";"]:
        base = base.replace(sep, " ")
    parts = [p for p in base.split(" ") if p]
    prefix = parts[-1] if parts else ""
    labels = await get_repo_labels_cached()
    cand = [l for l in labels if prefix.lower() in l.lower()]
    short_cand = [k for k in LABEL_SHORTCUTS.keys() if prefix.lower() in k.lower()]
    suggestions = (short_cand + cand)[:25]

    def replace_last(s: str) -> str:
        if not prefix:
            return s
        return (base[:-len(prefix)] + s).strip()

    return [app_commands.Choice(name=s, value=replace_last(s)) for s in suggestions]

# === 協力者補完 ===
_COLLAB_CACHE: Dict[str, List[str]] = {}
def _collab_cache_key() -> str:
    return f"{GH_OWNER}/{GH_REPO}"

async def get_repo_collaborators_cached() -> List[str]:
    key = _collab_cache_key()
    if key in _COLLAB_CACHE:
        return _COLLAB_CACHE[key]
    def _fetch():
        g = gh_client()
        repo = g.get_repo(f"{GH_OWNER}/{GH_REPO}")
        try:
            colls = [u.login for u in repo.get_collaborators(permission="push")]
        except Exception:
            colls = []
        try:
            issues = list(repo.get_issues(state="all"))[:200]
            colls.extend({(i.user.login if i.user else "") for i in issues})
        except Exception:
            pass
        colls = [c for c in set(colls) if c]
        colls.sort(key=str.lower)
        return colls
    logins = await asyncio.to_thread(_fetch)
    _COLLAB_CACHE[key] = logins
    return logins

async def autocomplete_assignee(interaction: discord.Interaction, current: str):
    cand = await get_repo_collaborators_cached()
    cur = (current or "").lower()
    filtered = [c for c in cand if cur in c.lower()][:25]
    items = ["me"] + filtered
    return [app_commands.Choice(name=x, value=x) for x in items[:25]]

async def autocomplete_group_name(interaction: discord.Interaction, current: str):
    ch = interaction.channel
    ch_id = ch.id if isinstance(ch, discord.TextChannel) else None
    names = []
    if ch_id:
        groups = await list_bundle_groups(ch_id)
        names = [g for g, _ in groups]
    cur = (current or "").lower()
    filtered = [n for n in names if cur in n.lower()][:25]
    return [app_commands.Choice(name=n, value=n) for n in filtered]

async def autocomplete_preset_name(interaction: discord.Interaction, current: str):
    names = await preset_list(current or "")
    return [app_commands.Choice(name=n, value=n) for n in names[:25]]

# ========= モーダル =========
class IssueCreateModal(discord.ui.Modal, title="Issueを作成"):
    # 5項目に減らす（title/body/assignee/labels/due）
    def __init__(self):
        super().__init__()
        self.title_input = discord.ui.TextInput(
            label="タイトル",
            placeholder="例) クリティカルバグ：タイトル画面でクラッシュ",
            required=True, max_length=256
        )
        self.body_input = discord.ui.TextInput(
            label="本文（任意）",
            style=discord.TextStyle.paragraph,
            placeholder="再現手順・期待動作・環境など",
            required=False, max_length=2000
        )
        self.assignee_input = discord.ui.TextInput(
            label="担当（任意）",
            placeholder="GitHubログイン名 or 'me'",
            required=False
        )
        self.labels_input = discord.ui.TextInput(
            label="ラベル（任意）",
            placeholder="todo doing #bug（スペース/カンマ区切り・ショートカット可）",
            required=False
        )
        self.due_input = discord.ui.TextInput(
            label="期日（任意）",
            placeholder="YYYY-MM-DD",
            required=False
        )
        for x in [self.title_input, self.body_input, self.assignee_input, self.labels_input, self.due_input]:
            self.add_item(x)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        title = str(self.title_input.value).strip()
        body = str(self.body_input.value or "").strip() or None
        assignee = str(self.assignee_input.value or "").strip() or None
        labels_s = str(self.labels_input.value or "").strip()
        due = str(self.due_input.value or "").strip() or None

        # 'me' を GitHub ログインに解決
        if assignee and assignee.lower() == "me":
            async with aiosqlite.connect(DB_PATH) as db:
                cur = await db.execute(
                    "SELECT github_login FROM user_link WHERE discord_user_id=?",
                    (interaction.user.id,)
                )
                row = await cur.fetchone()
                if not row:
                    await interaction.followup.send("まず /link_github で紐付けてください。", ephemeral=True)
                    return
                assignee = row[0]

        # ラベル正規化 & テンプレ自動判定
        tokens = normalize_label_input(labels_s)  # 例: ['status:todo','type:bug']
        template_key = None
        if any(t.lower() == "type:bug" for t in tokens):
            template_key = "bug"
        elif any(t.lower() == "type:task" for t in tokens):
            template_key = "task"
        elif any(t.lower() == "type:feature" for t in tokens):
            template_key = "feature"

        # テンプレ使用時は type:* を送信ラベルから外す（重複回避）
        if template_key:
            tokens = [t for t in tokens if not t.lower().startswith("type:")]

        labels_csv = ",".join(tokens) if tokens else None

        try:
            issue = await gh_create_issue_with_template(
                title=title,
                body=body,
                assignee=assignee,
                labels_csv=labels_csv,
                due=due,
                template_key=template_key
            )
            await interaction.followup.send(f"作成: [#{issue.number}] {issue.html_url}", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"失敗: {e}", ephemeral=True)

class GroupAddModal(discord.ui.Modal, title="グループを追加"):
    def __init__(self):
        super().__init__()
        self.name_input = discord.ui.TextInput(label="グループ名", placeholder="todo / bugs / mine など", required=True, max_length=50)
        self.labels_input = discord.ui.TextInput(label="ラベル", placeholder="todo doing #bug（スペース/カンマ区切り・ショートカット可）", required=False, max_length=200)
        self.add_item(self.name_input); self.add_item(self.labels_input)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        if not isinstance(interaction.channel, discord.TextChannel):
            await interaction.followup.send("テキストチャンネルで実行してください。", ephemeral=True); return
        ch = interaction.channel
        bundle = await get_bundle(ch.id)
        if not bundle:
            content = await build_bundle_content(ch.id)
            msg = await ch.send(content=content)
            try: await msg.edit(suppress=True)
            except TypeError:
                try: await msg.suppress_embeds(True)  # type: ignore[attr-defined]
                except Exception: pass
            try: await msg.pin()
            except discord.Forbidden: pass
            await upsert_bundle(ch.id, msg.id, DEFAULT_INTERVAL_MIN, True, True)
        name = str(self.name_input.value).strip()
        labels = normalize_label_input(str(self.labels_input.value or "")) if self.labels_input.value else []
        await upsert_bundle_group(ch.id, name, labels)
        if hasattr(interaction.client, "_bundle_last_refresh"):
            interaction.client._bundle_last_refresh[ch.id] = 0
        await interaction.followup.send(f"追加: グループ '{name}' -> {labels or 'なし'}", ephemeral=True)

# ======= 差し替え: GroupEditLabelsModal / GroupRenameModal / IntervalEditModal =======
class GroupEditLabelsModal(discord.ui.Modal, title="グループのラベルを編集"):
    def __init__(self, channel_id: int, group_name: str):
        super().__init__()
        self.channel_id = int(channel_id)
        self.group_name = group_name
        # 既存値のプリセットは send_modal 前に .default を設定（View側）
        self.labels_input = discord.ui.TextInput(
            label="ラベル",
            placeholder="todo doing #bug（スペース/カンマ区切り・ショートカット可）",
            required=False,
            max_length=200
        )
        self.add_item(self.labels_input)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        labels = normalize_label_input(str(self.labels_input.value or ""))
        await upsert_bundle_group(self.channel_id, self.group_name, labels)
        if hasattr(interaction.client, "_bundle_last_refresh"):
            interaction.client._bundle_last_refresh[self.channel_id] = 0
        await interaction.followup.send(f"更新: '{self.group_name}' -> {labels or 'なし'}", ephemeral=True)

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        await interaction.response.send_message(f"エラー: {error}", ephemeral=True)

class GroupRenameModal(discord.ui.Modal, title="グループ名を変更"):
    def __init__(self, channel_id: int, group_name: str):
        super().__init__()
        self.channel_id = int(channel_id)
        self.group_name = group_name
        self.new_name_input = discord.ui.TextInput(
            label="新しいグループ名",
            placeholder="例) today",
            required=True,
            max_length=50
        )
        self.add_item(self.new_name_input)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        new_name = str(self.new_name_input.value or "").strip()
        if not new_name:
            await interaction.followup.send("名称が空です。", ephemeral=True); return
        if new_name == self.group_name:
            await interaction.followup.send("同じ名前です。変更はありません。", ephemeral=True); return

        # 存在チェック・重複チェック
        groups = await list_bundle_groups(self.channel_id)
        names = {g for g, _ in groups}
        if self.group_name not in names:
            await interaction.followup.send("元のグループが見つかりません。", ephemeral=True); return
        if new_name in names:
            await interaction.followup.send("その名前は既に存在します。別名を指定してください。", ephemeral=True); return

        import sqlite3
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute(
                    "UPDATE bundle_group SET group_name=? WHERE channel_id=? AND group_name=?",
                    (new_name, self.channel_id, self.group_name)
                )
                await db.commit()
        except sqlite3.IntegrityError:
            await interaction.followup.send("一意制約エラー。別名を指定してください。", ephemeral=True); return

        if hasattr(interaction.client, "_bundle_last_refresh"):
            interaction.client._bundle_last_refresh[self.channel_id] = 0
        await interaction.followup.send(f"リネーム: '{self.group_name}' -> '{new_name}'", ephemeral=True)

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        await interaction.response.send_message(f"エラー: {error}", ephemeral=True)

class IntervalEditModal(discord.ui.Modal, title="更新間隔を変更"):
    def __init__(self, channel_id: int):
        super().__init__()
        self.channel_id = int(channel_id)
        # 既定値は send_modal 直前にセット（View側）
        self.iv_input = discord.ui.TextInput(
            label="間隔(分) 1〜180",
            placeholder="5",
            required=True,
            max_length=4
        )
        self.add_item(self.iv_input)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            iv = int(str(self.iv_input.value).strip())
            if iv < 1 or iv > 180:
                raise ValueError
        except Exception:
            await interaction.followup.send("1〜180 の整数で指定してください。", ephemeral=True); return

        bundle = await get_bundle(self.channel_id)
        if not bundle:
            await interaction.followup.send("バンドルが未作成です。/task_bind_bundle を先に実行。", ephemeral=True); return

        _, msg_id, _, pin, sup = bundle
        await upsert_bundle(self.channel_id, msg_id, iv, pin, sup)
        if hasattr(interaction.client, "_bundle_last_refresh"):
            interaction.client._bundle_last_refresh[self.channel_id] = 0
        await interaction.followup.send(f"更新: interval={iv}分", ephemeral=True)

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        await interaction.response.send_message(f"エラー: {error}", ephemeral=True)

# ========= UI View（セレクト＋ボタン） =========
class GroupsManageView(discord.ui.View):
    def __init__(self, channel_id: int, *, timeout: int = 300):
        super().__init__(timeout=timeout)
        self.channel_id = channel_id
        self._selected: Optional[str] = None

    async def refresh_options(self, interaction: discord.Interaction):
        groups = await list_bundle_groups(self.channel_id)
        options = [discord.SelectOption(label=g, description=" ,".join(labs)[:90] if labs else "なし") for g, labs in groups]
        if not options:
            options = [discord.SelectOption(label="（なし）", description="まず /task_group_add またはモーダルで追加")]
        for child in self.children:
            if isinstance(child, discord.ui.Select):
                child.options = options
                break
        # 再描画
        await interaction.response.edit_message(view=self)

    @discord.ui.select(placeholder="グループを選択", min_values=1, max_values=1, options=[discord.SelectOption(label="読み込み中", description="...")])
    async def group_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        self._selected = select.values[0]
        await interaction.response.defer()  # UI選択のみ

    @discord.ui.button(label="ラベル編集", style=discord.ButtonStyle.primary)
    async def btn_edit_labels(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self._selected in (None, "（なし）"):
            await interaction.response.send_message("先にグループを選択。", ephemeral=True)
            return
        # 既存ラベルを取得して default に注入
        groups = await list_bundle_groups(self.channel_id)
        current_labels: List[str] = []
        for g, labs in groups:
            if g == self._selected:
                current_labels = labs or []
                break
        modal = GroupEditLabelsModal(self.channel_id, self._selected)
        modal.labels_input.default = " ".join(current_labels)  # 送る前に default セット
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="リネーム", style=discord.ButtonStyle.secondary)
    async def btn_rename(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self._selected in (None, "（なし）"):
            await interaction.response.send_message("先にグループを選択。", ephemeral=True); return
        modal = GroupRenameModal(self.channel_id, self._selected)
        modal.new_name_input.placeholder = f"現在: {self._selected}"
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="削除", style=discord.ButtonStyle.danger)
    async def btn_delete(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self._selected in (None, "（なし）"):
            await interaction.response.send_message("先にグループを選択。", ephemeral=True); return
        ok = await delete_bundle_group(self.channel_id, self._selected)
        # 即時UI更新
        await self.refresh_options(interaction)
        # 次ループで再描画
        if hasattr(interaction.client, "_bundle_last_refresh"):
            interaction.client._bundle_last_refresh[self.channel_id] = 0
        await interaction.followup.send(f"削除: '{self._selected}'（結果: {'OK' if ok else '無し'}）", ephemeral=True)

    @discord.ui.button(label="今すぐ更新", style=discord.ButtonStyle.success)
    async def btn_refresh_now(self, interaction: discord.Interaction, button: discord.ui.Button):
        bundle = await get_bundle(self.channel_id)
        if not bundle:
            await interaction.response.send_message("バンドル未作成。/task_bind_bundle を先に実行。", ephemeral=True); return
        _, msg_id, _, pin, sup = bundle
        await refresh_bundle_message(interaction.client, self.channel_id, msg_id, pin, sup)
        if hasattr(interaction.client, "_bundle_last_refresh"):
            interaction.client._bundle_last_refresh[self.channel_id] = 0
        await interaction.response.send_message("更新しました。", ephemeral=True)

    @discord.ui.button(label="PIN切替", style=discord.ButtonStyle.secondary)
    async def btn_toggle_pin(self, interaction: discord.Interaction, button: discord.ui.Button):
        bundle = await get_bundle(self.channel_id)
        if not bundle:
            await interaction.response.send_message("バンドル未作成。/task_bind_bundle を先に実行。", ephemeral=True); return
        ch_id, msg_id, iv, pin, sup = bundle
        pin = not pin
        await upsert_bundle(ch_id, msg_id, iv, pin, sup)
        try:
            m = await interaction.channel.fetch_message(msg_id)
            if pin and not m.pinned:
                try: await m.pin()
                except discord.Forbidden: pass
            if not pin and m.pinned:
                try: await m.unpin()
                except discord.Forbidden: pass
        except Exception:
            pass
        await interaction.response.send_message(f"PIN: {pin}", ephemeral=True)

    @discord.ui.button(label="プレビュー抑止切替", style=discord.ButtonStyle.secondary)
    async def btn_toggle_suppress(self, interaction: discord.Interaction, button: discord.ui.Button):
        bundle = await get_bundle(self.channel_id)
        if not bundle:
            await interaction.response.send_message("バンドル未作成。/task_bind_bundle を先に実行。", ephemeral=True); return
        ch_id, msg_id, iv, pin, sup = bundle
        sup = not sup
        await upsert_bundle(ch_id, msg_id, iv, pin, sup)
        try:
            m = await interaction.channel.fetch_message(msg_id)
            if sup:
                try: await m.edit(suppress=True)
                except TypeError:
                    try: await m.suppress_embeds(True)  # type: ignore[attr-defined]
                    except Exception: pass
            else:
                # 解除APIは無い。再編集時に embeds を含まない限り影響は軽微。
                pass
        except Exception:
            pass
        await interaction.response.send_message(f"suppress: {sup}", ephemeral=True)

    @discord.ui.button(label="間隔変更(モーダル)", style=discord.ButtonStyle.secondary)
    async def btn_interval_modal(self, interaction: discord.Interaction, button: discord.ui.Button):
        modal = IntervalEditModal(self.channel_id)
        bundle = await get_bundle(self.channel_id)
        if bundle:
            _, _, iv, _, _ = bundle
            modal.iv_input.default = str(iv)
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="グループ追加(モーダル)", style=discord.ButtonStyle.primary)
    async def btn_group_add_modal(self, interaction: discord.Interaction, button: discord.ui.Button):
        modal = GroupAddModal()
        await interaction.response.send_modal(modal)

# ====== Discord クライアント ======
class Bot(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)
        self._bundle_last_refresh: Dict[int, int] = {}  # channel_id -> epoch
        self._task_list_last_message: Dict[int, int] = {}

    # --- コマンド定義 ---
    def define_link_github(self):
        @self.tree.command(name="link_github", description="GitHubアカウントを自分のDiscordユーザーに紐付けます。")
        @app_commands.describe(login="GitHubのログイン名（例: octocat）")
        async def link_github_cmd(interaction: discord.Interaction, login: str):
            await interaction.response.defer(ephemeral=True)
            try:
                g = gh_client()
                g.get_user(login).id
            except Exception:
                await interaction.followup.send("GitHubユーザーが見つかりません。スペルを確認してください。", ephemeral=True)
                return
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute(
                    "INSERT INTO user_link (discord_user_id, github_login) VALUES (?, ?) "
                    "ON CONFLICT(discord_user_id) DO UPDATE SET github_login=excluded.github_login",
                    (interaction.user.id, login)
                )
                await db.commit()
            await interaction.followup.send(f"Linked: {login}", ephemeral=True)

    def define_task_add(self):
        @self.tree.command(name="task_add", description="GitHub Issue を作成（テンプレ/期日/ラベル対応）。")
        @app_commands.describe(
            title="タイトル",
            body="本文（任意）",
            assignee="担当。'me' で自分（補完可）",
            labels="ラベルCSV（例: type:bug,status:todo）",
            due="期日 YYYY-MM-DD（任意）",
            template="テンプレ選択"
        )
        @app_commands.choices(template=[
            app_commands.Choice(name="Bug", value="bug"),
            app_commands.Choice(name="Task", value="task"),
            app_commands.Choice(name="Feature", value="feature"),
        ])
        @app_commands.autocomplete(assignee=autocomplete_assignee)
        async def task_add_cmd(
            interaction: discord.Interaction,
            title: str,
            body: Optional[str] = None,
            assignee: Optional[str] = None,
            labels: Optional[str] = None,
            due: Optional[str] = None,
            template: Optional[app_commands.Choice[str]] = None
        ):
            await interaction.response.defer()
            if assignee and assignee.lower() == "me":
                async with aiosqlite.connect(DB_PATH) as db:
                    cur = await db.execute("SELECT github_login FROM user_link WHERE discord_user_id=?", (interaction.user.id,))
                    row = await cur.fetchone()
                    if not row:
                        await interaction.followup.send("まず /link_github で紐付けてください。", ephemeral=True)
                        return
                    assignee = row[0]
            templ_val = template.value if isinstance(template, app_commands.Choice) else None
            try:
                issue = await gh_create_issue_with_template(title, body, assignee, labels, due, templ_val)
                await interaction.followup.send(f"作成: [#{issue.number}] {issue.html_url}")
            except ValueError as ve:
                await interaction.followup.send(f"エラー: {ve}", ephemeral=True)
            except Exception as e:
                await interaction.followup.send(f"失敗: {e}", ephemeral=True)

    def define_task_add_modal(self):
        @self.tree.command(name="task_add_modal", description="モーダルでIssue作成（タイトル/本文/担当/ラベル/期日/テンプレ）。")
        async def task_add_modal_cmd(interaction: discord.Interaction):
            modal = IssueCreateModal()
            await interaction.response.send_modal(modal)

    def define_task_unblock(self):
        @self.tree.command(name="task_unblock", description="Issueのstatus:blockedを解除します。コメント追記可。")
        @app_commands.describe(number="Issue番号", reason="解除理由（任意）")
        async def task_unblock_cmd(interaction: discord.Interaction, number: int, reason: Optional[str] = None):
            await interaction.response.defer(ephemeral=True)
            note = (reason or "").strip()
            user_display = getattr(interaction.user, "display_name", str(interaction.user))

            def worker(issue: GH_Issue):
                labels = [l.name for l in issue.labels]
                if not any(l.lower() == "status:blocked" for l in labels):
                    return ("not_blocked", issue.html_url)
                updated = remove_label(labels, "status:blocked")
                if not any(l.lower().startswith("status:") for l in updated):
                    updated.append("status:todo")
                issue.edit(labels=updated)
                comment_lines = [f"[unblock] Discordからブロック解除 (by {user_display})"]
                if note:
                    comment_lines.append("")
                    comment_lines.append(note)
                issue.create_comment("\n".join(comment_lines))
                return ("unblocked", issue.html_url)

            try:
                state, url = await run_issue_action(number, worker)
            except GithubException as e:
                await interaction.followup.send(f"GitHubエラー: {e}", ephemeral=True)
                return
            except Exception as e:
                await interaction.followup.send(f"失敗: {e}", ephemeral=True)
                return

            if state == "not_blocked":
                await interaction.followup.send(f"[#{number}] は status:blocked が付与されていません。 {url}", ephemeral=True)
            else:
                await interaction.followup.send(f"解除しました: [#{number}] {url}", ephemeral=True)

    def define_task_reopen(self):
        @self.tree.command(name="task_reopen", description="Close済みIssueを再Openしてstatus:todoに戻します。")
        @app_commands.describe(number="Issue番号")
        async def task_reopen_cmd(interaction: discord.Interaction, number: int):
            await interaction.response.defer(ephemeral=True)

            def worker(issue: GH_Issue):
                labels = [l.name for l in issue.labels]
                new_labels = replace_status_label(labels, "status:todo")
                already_open = issue.state == "open"
                issue.edit(state="open", labels=new_labels)
                return (already_open, issue.html_url)

            try:
                was_open, url = await run_issue_action(number, worker)
            except GithubException as e:
                await interaction.followup.send(f"GitHubエラー: {e}", ephemeral=True)
                return
            except Exception as e:
                await interaction.followup.send(f"失敗: {e}", ephemeral=True)
                return

            if was_open:
                await interaction.followup.send(f"[#{number}] は既にOpenでした。 {url}", ephemeral=True)
            else:
                await interaction.followup.send(f"再Openしました: [#{number}] {url}", ephemeral=True)

    def define_task_assign(self):
        @self.tree.command(name="task_assign", description="Issueに担当者を割り当てます。")
        @app_commands.describe(number="Issue番号", user="GitHubログイン、または'me'")
        @app_commands.autocomplete(user=autocomplete_assignee)
        async def task_assign_cmd(interaction: discord.Interaction, number: int, user: str):
            await interaction.response.defer(ephemeral=True)
            raw_user = (user or "").strip()
            if not raw_user:
                await interaction.followup.send("ユーザーを指定してください。", ephemeral=True)
                return
            resolved = await resolve_login_input(raw_user, interaction.user.id)
            if raw_user.lower() == "me" and not resolved:
                await interaction.followup.send("まず /link_github でGitHubアカウントを紐付けてください。", ephemeral=True)
                return
            login = resolved or raw_user

            def worker(issue: GH_Issue):
                labels = [l.name for l in issue.labels]
                new_labels = replace_status_label(labels, "status:in_progress")
                assignees = [a.login for a in issue.assignees if a]
                if login not in assignees:
                    assignees.append(login)
                issue.edit(labels=new_labels, assignees=assignees)
                return (issue.html_url, assignees)

            try:
                url, assignees = await run_issue_action(number, worker)
            except GithubException as e:
                await interaction.followup.send(f"GitHubエラー: {e}", ephemeral=True)
                return
            except Exception as e:
                await interaction.followup.send(f"失敗: {e}", ephemeral=True)
                return

            await interaction.followup.send(f"担当: [#{number}] -> {', '.join(assignees)} {url}", ephemeral=True)

    def define_task_comment(self):
        @self.tree.command(name="task_comment", description="GitHub Issue にコメントを追加します。")
        @app_commands.describe(number="Issue番号", comment="コメント本文")
        async def task_comment_cmd(interaction: discord.Interaction, number: int, comment: str):
            body = (comment or "").strip()
            if not body:
                await interaction.response.send_message("コメントを入力してください。", ephemeral=True)
                return
            await interaction.response.defer(ephemeral=True)
            user_display = getattr(interaction.user, "display_name", str(interaction.user))

            def worker(issue: GH_Issue):
                comment_lines = [body, "", "---", f"Discord: {user_display}"]
                issue.create_comment("\n".join(comment_lines))
                return issue.html_url

            try:
                url = await run_issue_action(number, worker)
            except GithubException as e:
                await interaction.followup.send(f"GitHubエラー: {e}", ephemeral=True)
                return
            except Exception as e:
                await interaction.followup.send(f"失敗: {e}", ephemeral=True)
                return

            await interaction.followup.send(f"コメントを追加しました: [#{number}] {url}", ephemeral=True)

    def define_task_search(self):
        @self.tree.command(name="task_search", description="ラベルやキーワードでIssueを検索します。")
        @app_commands.describe(label="ラベル（空白・カンマ区切り可）", keyword="キーワード（部分一致）")
        @app_commands.autocomplete(label=autocomplete_labels)
        async def task_search_cmd(interaction: discord.Interaction, label: Optional[str] = None, keyword: Optional[str] = None):
            await interaction.response.defer(ephemeral=True)
            labels = normalize_label_input(label or "") if label else []
            query_parts = [f"repo:{GH_OWNER}/{GH_REPO}"]
            for lab in labels:
                quoted = f'"{lab}"' if " " in lab else lab
                query_parts.append(f"label:{quoted}")
            if keyword:
                kw = keyword.strip()
                if kw:
                    if " " in kw and not (kw.startswith('"') and kw.endswith('"')):
                        kw = f'"{kw}"'
                    query_parts.append(kw)
            query = " ".join(query_parts)

            def worker():
                g = gh_client()
                result = g.search_issues(query, sort="updated", order="desc")
                return list(result[:10]), result.totalCount if hasattr(result, 'totalCount') else None

            try:
                issues, total = await asyncio.to_thread(worker)
            except GithubException as e:
                await interaction.followup.send(f"GitHubエラー: {e}", ephemeral=True)
                return
            except Exception as e:
                await interaction.followup.send(f"検索に失敗しました: {e}", ephemeral=True)
                return

            if not issues:
                await interaction.followup.send("該当するIssueはありませんでした。", ephemeral=True)
                return

            header = f"検索クエリ: {query}"
            if total is not None:
                header += f"\nヒット件数: {total}件"
            blocks = [header, ""]
            for issue in issues:
                blocks.append(render_issue_block(issue))
                blocks.append("")
            message = "\n".join(blocks).strip()
            await interaction.followup.send(message[:DISCORD_MSG_LIMIT], ephemeral=True)

    def define_task_status(self):
        @self.tree.command(name="task_status", description="statusラベルごとの進捗サマリを表示します。")
        async def task_status_cmd(interaction: discord.Interaction):
            await interaction.response.defer(ephemeral=True)

            def worker():
                repo = gh_client().get_repo(f"{GH_OWNER}/{GH_REPO}")
                return list(repo.get_issues(state="open")[:200])  # ※ここは次項参照

            try:
                issues = await asyncio.to_thread(worker)
            except GithubException as e:
                await interaction.followup.send(f"GitHubエラー: {e}", ephemeral=True)
                return
            except Exception as e:
                await interaction.followup.send(f"取得に失敗しました: {e}", ephemeral=True)
                return

            buckets = {"todo": [], "in_progress": [], "done": [], "others": []}
            for issue in issues:
                if has_label(issue, "status:todo"):
                    buckets["todo"].append(issue)
                elif has_label(issue, "status:in_progress"):
                    buckets["in_progress"].append(issue)
                elif has_label(issue, "status:done"):
                    buckets["done"].append(issue)
                else:
                    buckets["others"].append(issue)
            for arr in buckets.values():
                arr.sort(key=lambda i: i.updated_at, reverse=True)

            order = [
                ("todo", "未着手 (status:todo)"),
                ("in_progress", "進行中 (status:in_progress)"),
                ("done", "完了想定 (status:done)"),
                ("others", "未分類"),
            ]
            total = sum(len(v) for v in buckets.values())
            parts = [f"Open Issue総数: {total}件"]
            for key, title in order:
                arr = buckets[key]
                parts.append("")
                parts.append(f"**{title}** ({len(arr)}件)")
                if not arr:
                    parts.append("> 該当なし")
                    continue
                for issue in arr[:5]:
                    parts.append(render_issue_block(issue))
                    parts.append("")
                if len(arr) > 5:
                    parts.append(f"> ...ほか {len(arr) - 5} 件")
            message = "\n".join(parts).strip()  # ← blocks -> parts に修正
            await interaction.followup.send(message[:DISCORD_MSG_LIMIT], ephemeral=True)


    def define_task_claim(self):
        @self.tree.command(name="task_claim", description="自分を担当者に設定し、進行中に変更します。")
        @app_commands.describe(number="Issue番号", note="補足コメント（任意）")
        async def task_claim_cmd(interaction: discord.Interaction, number: int, note: Optional[str] = None):
            login = await get_linked_login(interaction.user.id)
            if not login:
                await interaction.response.send_message("まず /link_github でGitHubアカウントを紐付けてください。", ephemeral=True)
                return
            await interaction.response.defer(ephemeral=True)
            memo = (note or "").strip()
            user_display = getattr(interaction.user, "display_name", str(interaction.user))

            def worker(issue: GH_Issue):
                labels = [l.name for l in issue.labels]
                new_labels = replace_status_label(labels, "status:in_progress")
                assignees = [a.login for a in issue.assignees if a]
                if login not in assignees:
                    assignees.append(login)
                issue.edit(labels=new_labels, assignees=assignees)
                comment_lines = [f"[claim] {login} が担当を宣言 (by {user_display})"]
                if memo:
                    comment_lines.append("")
                    comment_lines.append(memo)
                issue.create_comment("\n".join(comment_lines))
                return issue.html_url

            try:
                url = await run_issue_action(number, worker)
            except GithubException as e:
                await interaction.followup.send(f"GitHubエラー: {e}", ephemeral=True)
                return
            except Exception as e:
                await interaction.followup.send(f"失敗: {e}", ephemeral=True)
                return

            await interaction.followup.send(f"担当を宣言しました: [#{number}] {url}", ephemeral=True)

    def define_task_done(self):
        @self.tree.command(name="task_done", description="Issueを完了扱いにし、必要ならCloseします。")
        @app_commands.describe(number="Issue番号", close="IssueをCloseするか", note="補足コメント（任意）")
        async def task_done_cmd(
            interaction: discord.Interaction,
            number: int,
            close: bool = True,
            note: Optional[str] = None,
        ):
            await interaction.response.defer(ephemeral=True)
            memo = (note or "").strip()
            user_display = getattr(interaction.user, "display_name", str(interaction.user))

            def worker(issue: GH_Issue):
                labels = [l.name for l in issue.labels]
                new_labels = replace_status_label(labels, "status:done")
                kwargs = {"labels": new_labels}
                if close:
                    kwargs["state"] = "closed"
                issue.edit(**kwargs)
                comment_lines = [f"[done] Discordから完了処理 (by {user_display})"]
                if memo:
                    comment_lines.append("")
                    comment_lines.append(memo)
                if close:
                    comment_lines.append("")
                    comment_lines.append("IssueをCloseしました。")
                issue.create_comment("\n".join(comment_lines))
                return issue.html_url

            try:
                url = await run_issue_action(number, worker)
            except GithubException as e:
                await interaction.followup.send(f"GitHubエラー: {e}", ephemeral=True)
                return
            except Exception as e:
                await interaction.followup.send(f"失敗: {e}", ephemeral=True)
                return

            action = "完了＋Close" if close else "完了"
            await interaction.followup.send(f"{action}にしました: [#{number}] {url}", ephemeral=True)

    def define_task_unclaim(self):
        @self.tree.command(name="task_unclaim", description="自分の担当を外し、status:todoに戻します。")
        @app_commands.describe(number="Issue番号", note="補足コメント（任意）")
        async def task_unclaim_cmd(interaction: discord.Interaction, number: int, note: Optional[str] = None):
            login = await get_linked_login(interaction.user.id)
            if not login:
                await interaction.response.send_message("まず /link_github でGitHubアカウントを紐付けてください。", ephemeral=True)
                return
            await interaction.response.defer(ephemeral=True)
            memo = (note or "").strip()
            user_display = getattr(interaction.user, "display_name", str(interaction.user))

            def worker(issue: GH_Issue):
                labels = [l.name for l in issue.labels]
                new_labels = replace_status_label(labels, "status:todo")
                original = [a.login for a in issue.assignees if a]
                if login not in original:
                    return ("not_assigned", issue.html_url)
                assignees = [a for a in original if a != login]
                issue.edit(labels=new_labels, assignees=assignees)
                comment_lines = [f"[unclaim] {login} が担当を辞退 (by {user_display})"]
                if memo:
                    comment_lines.append("")
                    comment_lines.append(memo)
                issue.create_comment("\n".join(comment_lines))
                return ("unclaimed", issue.html_url)

            try:
                state, url = await run_issue_action(number, worker)
            except GithubException as e:
                await interaction.followup.send(f"GitHubエラー: {e}", ephemeral=True)
                return
            except Exception as e:
                await interaction.followup.send(f"失敗: {e}", ephemeral=True)
                return

            if state == "not_assigned":
                await interaction.followup.send(f"[#{number}] は {login} が担当者ではありません。 {url}", ephemeral=True)
            else:
                await interaction.followup.send(f"担当を外しました: [#{number}] {url}", ephemeral=True)


    # ===== バンドル =====
    def define_task_bind_bundle(self):
        @self.tree.command(name="task_bind_bundle", description="このチャンネルに『一覧バンドル』メッセージを作成（1メッセージに複数グループ）。")
        @app_commands.describe(
            interval="更新間隔(分) 1〜180。未指定は既定。",
            pin="メッセージをピン留め（既定: 有効）",
            suppress="リンクプレビューを消す（既定: 有効）",
            interval_quick="クイック選択（1/3/5/10/15）"
        )
        @app_commands.choices(interval_quick=[
            app_commands.Choice(name="1分", value=1),
            app_commands.Choice(name="3分", value=3),
            app_commands.Choice(name="5分", value=5),
            app_commands.Choice(name="10分", value=10),
            app_commands.Choice(name="15分", value=15),
        ])
        async def task_bind_bundle_cmd(
            interaction: discord.Interaction,
            interval: Optional[app_commands.Range[int, 1, 180]] = None,
            pin: Optional[bool] = True,
            suppress: Optional[bool] = True,
            interval_quick: Optional[app_commands.Choice[int]] = None,
        ):
            await interaction.response.defer(ephemeral=True)
            if not isinstance(interaction.channel, discord.TextChannel):
                await interaction.followup.send("テキストチャンネルで実行してください。", ephemeral=True); return
            ch = interaction.channel
            iv = interval_quick.value if isinstance(interval_quick, app_commands.Choice) else (int(interval) if interval is not None else DEFAULT_INTERVAL_MIN)

            content = await build_bundle_content(ch.id)
            msg = await ch.send(content=content)
            if suppress:
                try: await msg.edit(suppress=True)
                except TypeError:
                    try: await msg.suppress_embeds(True)  # type: ignore[attr-defined]
                    except Exception: pass
            if pin:
                try: await msg.pin()
                except discord.Forbidden: pass
            await upsert_bundle(ch.id, msg.id, iv, bool(pin), bool(suppress))
            self._bundle_last_refresh[ch.id] = 0
            await interaction.followup.send(
                f"OK: バンドル作成 interval={iv}分, pin={bool(pin)}, suppress={bool(suppress)}。グループは `/task_group_add` or `/task_group_add_modal` で追加。",
                ephemeral=True
            )

    def define_task_group_add(self):
        @self.tree.command(name="task_group_add", description="バンドルにグループ（セクション）を追加します。")
        @app_commands.describe(
            name="グループ名（例: todo, bugs, mine）",
            label_filters="ラベル。スペース/カンマ区切り・ショートカット可（todo/doing/done, #bug/#task/#feature）",
            channel="対象チャンネル（省略時は現在）",
        )
        @app_commands.autocomplete(label_filters=autocomplete_labels)
        async def task_group_add_cmd(interaction: discord.Interaction, name: str, label_filters: Optional[str] = None, channel: Optional[discord.TextChannel] = None):
            await interaction.response.defer(ephemeral=True)
            target_ch = channel or interaction.channel
            if not isinstance(target_ch, discord.TextChannel):
                await interaction.followup.send("テキストチャンネルを指定してください。", ephemeral=True); return
            bundle = await get_bundle(target_ch.id)
            if not bundle:
                content = await build_bundle_content(target_ch.id)
                msg = await target_ch.send(content=content)
                try: await msg.edit(suppress=True)
                except TypeError:
                    try: await msg.suppress_embeds(True)  # type: ignore[attr-defined]
                    except Exception: pass
                try: await msg.pin()
                except discord.Forbidden: pass
                await upsert_bundle(target_ch.id, msg.id, DEFAULT_INTERVAL_MIN, True, True)
            filters = normalize_label_input(label_filters or "")
            await upsert_bundle_group(target_ch.id, name, filters)
            self._bundle_last_refresh[target_ch.id] = 0
            await interaction.followup.send(f"追加: {target_ch.mention} のグループ '{name}' -> {filters or 'なし'}。", ephemeral=True)

    def define_task_group_add_modal(self):
        @self.tree.command(name="task_group_add_modal", description="モーダルでグループ追加（名前とラベルを入力）。")
        async def task_group_add_modal_cmd(interaction: discord.Interaction):
            modal = GroupAddModal()
            await interaction.response.send_modal(modal)

    def define_task_unbind_tag(self):
        @self.tree.command(name="task_group_remove", description="バンドルからグループを削除します。")
        @app_commands.describe(name="グループ名", channel="対象チャンネル（省略時は現在）")
        @app_commands.autocomplete(name=autocomplete_group_name)
        async def task_group_remove_cmd(interaction: discord.Interaction, name: str, channel: Optional[discord.TextChannel] = None):
            await interaction.response.defer(ephemeral=True)
            target_ch = channel or interaction.channel
            if not isinstance(target_ch, discord.TextChannel):
                await interaction.followup.send("テキストチャンネルを指定してください。", ephemeral=True); return
            ok = await delete_bundle_group(target_ch.id, name)
            if ok:
                self._bundle_last_refresh[target_ch.id] = 0
            await interaction.followup.send(f"削除: グループ '{name}'（結果: {'OK' if ok else '無し'}）", ephemeral=True)

    def define_task_groups(self):
        @self.tree.command(name="task_groups", description="このチャンネルのバンドル設定とグループ一覧を表示します。")
        async def task_groups_cmd(interaction: discord.Interaction):
            await interaction.response.defer(ephemeral=True)
            if not isinstance(interaction.channel, discord.TextChannel):
                await interaction.followup.send("テキストチャンネルで実行してください。", ephemeral=True); return
            ch = interaction.channel
            bundle = await get_bundle(ch.id)
            groups = await list_bundle_groups(ch.id)
            if not bundle and not groups:
                await interaction.followup.send("登録なし。まず `/task_bind_bundle` → `/task_group_add` を実行。", ephemeral=True); return
            embed = discord.Embed(title=f"バンドル設定 @ {ch.name}", color=0x2b90d9)
            if bundle:
                _, msg_id, iv, pin, sup = bundle
                embed.add_field(name="Bundle", value=f"message_id={msg_id} / interval={iv}m / pin={pin} / suppress={sup}", inline=False)
            else:
                embed.add_field(name="Bundle", value="未作成（/task_bind_bundle）", inline=False)
            if groups:
                for name, labs in groups:
                    embed.add_field(name=f"Group: {name}", value=f"labels={labs or 'なし'}", inline=False)
            else:
                embed.add_field(name="Groups", value="なし（/task_group_add で追加）", inline=False)
            await interaction.followup.send(embed=embed, ephemeral=True)

    def define_task_groups_edit(self):
        @self.tree.command(name="task_groups_edit", description="バンドル（interval/pin/suppress）やグループ（name/labels）を編集。指定項目だけ変更。")
        @app_commands.describe(
            bundle_interval="バンドル更新間隔(分) 1〜180（未指定は変更なし）",
            bundle_pin="ピン留め（true/false）",
            bundle_suppress="リンクプレビュー抑止（true/false）",
            name="対象グループ名（グループ編集時は必須）",
            label_filters="そのグループのラベル（スペース/カンマ区切り。ショートカット可）",
            new_name="グループ名の変更（任意）",
            channel="対象チャンネル（省略時は現在）",
        )
        @app_commands.autocomplete(name=autocomplete_group_name, label_filters=autocomplete_labels)
        async def task_groups_edit_cmd(
            interaction: discord.Interaction,
            bundle_interval: Optional[app_commands.Range[int, 1, 180]] = None,
            bundle_pin: Optional[bool] = None,
            bundle_suppress: Optional[bool] = None,
            name: Optional[str] = None,
            label_filters: Optional[str] = None,
            new_name: Optional[str] = None,
            channel: Optional[discord.TextChannel] = None,
        ):
            await interaction.response.defer(ephemeral=True)
            target_ch = channel or interaction.channel
            if not isinstance(target_ch, discord.TextChannel):
                await interaction.followup.send("テキストチャンネルを指定してください。", ephemeral=True); return
            bundle = await get_bundle(target_ch.id)
            if bundle:
                ch_id, msg_id, iv, pin, sup = bundle
                changed = False
                if bundle_interval is not None: iv = int(bundle_interval); changed = True
                if bundle_pin is not None: pin = bool(bundle_pin); changed = True
                if bundle_suppress is not None: sup = bool(bundle_suppress); changed = True
                if changed:
                    await upsert_bundle(ch_id, msg_id, iv, pin, sup)
                    try:
                        m = await target_ch.fetch_message(msg_id)
                        if sup:
                            try: await m.edit(suppress=True)
                            except TypeError:
                                try: await m.suppress_embeds(True)  # type: ignore[attr-defined]
                                except Exception: pass
                        if pin and not m.pinned:
                            try: await m.pin()
                            except discord.Forbidden: pass
                        if not pin and m.pinned:
                            try: await m.unpin()
                            except discord.Forbidden: pass
                    except Exception:
                        pass
            if name or label_filters or new_name:
                if not name:
                    await interaction.followup.send("グループ編集には name が必要です。", ephemeral=True); return
                groups = await list_bundle_groups(target_ch.id)
                if not any(gname == name for gname, _ in groups):
                    await interaction.followup.send("指定グループが見つかりません。", ephemeral=True); return
                if label_filters is not None:
                    filters = normalize_label_input(label_filters)
                    await upsert_bundle_group(target_ch.id, name, filters)
                if new_name:
                    async with aiosqlite.connect(DB_PATH) as db:
                        await db.execute("UPDATE bundle_group SET group_name=? WHERE channel_id=? AND group_name=?", (new_name, target_ch.id, name))
                        await db.commit()
            self._bundle_last_refresh[target_ch.id] = 0
            await interaction.followup.send("編集完了。", ephemeral=True)

    async def _collect_task_issues(
        self,
        channel: Optional[discord.abc.GuildChannel],
        status: Optional[app_commands.Choice[str]],
        assignee: Optional[str],
    ) -> List[GH_Issue]:
        base_channel: Optional[discord.TextChannel]
        if isinstance(channel, discord.TextChannel):
            base_channel = channel
        elif isinstance(channel, discord.Thread) and isinstance(channel.parent, discord.TextChannel):
            base_channel = channel.parent
        else:
            base_channel = None

        groups = await list_bundle_groups(base_channel.id) if base_channel else []
        filters_default = groups[0][1] if groups else []
        issues = await asyncio.to_thread(lambda: fetch_issues_sync(filters_default))
        status_raw = (status.value if isinstance(status, app_commands.Choice) else "todo,in_progress").lower()
        want = {x.strip() for x in status_raw.split(",") if x.strip() and x.strip() != "all"}

        def pick(issue: GH_Issue) -> bool:
            if assignee and (not issue.assignee or issue.assignee.login != assignee):
                return False
            if "done" in want and has_label(issue, "status:done"):
                return True
            if "in_progress" in want and issue.state == "open" and has_label(issue, "status:in_progress"):
                return True
            if "todo" in want and issue.state == "open" and has_label(issue, "status:todo"):
                return True
            return False

        target = [issue for issue in issues if pick(issue)]

        today = date.today()

        def rank(issue: GH_Issue) -> Tuple[int, datetime]:
            due = parse_due(issue)
            if due is None:
                urgency = 3
            elif due < today:
                urgency = 0
            elif due == today:
                urgency = 1
            elif (due - today).days <= 3:
                urgency = 2
            else:
                urgency = 3
            return (urgency, issue.updated_at)

        target.sort(key=rank)
        return target

    async def _send_task_list_embed(
        self,
        channel: Union[discord.TextChannel, discord.Thread],
        entries: List[str],
        title: str,
        *,
        page_idx: int = 0,
        per_page: int = TASK_LIST_PAGE_SIZE,
    ) -> discord.Message:
        view = TaskListView(self, entries, per_page=per_page, title=title)
        if view.page_total:
            view.page_idx = max(0, min(page_idx, view.page_total - 1))
        msg = await channel.send(embed=view.current_embed(), view=view)
        old_id = self._task_list_last_message.get(channel.id)
        if old_id and old_id != msg.id:
            try:
                old_msg = await channel.fetch_message(old_id)
                bot_user = self.user
                if bot_user and old_msg.author.id == bot_user.id:
                    await old_msg.delete()
            except (discord.NotFound, discord.Forbidden):
                pass
        self._task_list_last_message[channel.id] = msg.id
        return msg
    def define_task_list(self):
        @self.tree.command(name="task_list", description="簡易一覧（バンドルとは独立）。")
        @app_commands.describe(assignee="担当で絞り込み（任意）")
        @app_commands.choices(
            status=[
                app_commands.Choice(name="todo", value="todo"),
                app_commands.Choice(name="in_progress", value="in_progress"),
                app_commands.Choice(name="done", value="done"),
                app_commands.Choice(name="all", value="all"),
            ]
        )
        @app_commands.autocomplete(assignee=autocomplete_assignee)
        async def task_list_cmd(
            interaction: discord.Interaction,
            status: Optional[app_commands.Choice[str]] = None,
            assignee: Optional[str] = None,
        ):
            await interaction.response.defer(ephemeral=True)
            issues = await self._collect_task_issues(interaction.channel, status, assignee)
            top = issues[:20]
            if not top:
                await interaction.followup.send("該当なし。", ephemeral=True)
                return

            embed = discord.Embed(title="タスク一覧（簡易）", color=TASK_LIST_EMBED_COLOR)
            for issue in top:
                mark = decorate_due_marker(issue)
                embed.add_field(name=f"#{issue.number} {issue.title}{mark}", value=f"{issue.html_url}", inline=False)
            await interaction.followup.send(embed=embed, ephemeral=True)
    def define_task_list_embed(self):
        @self.tree.command(name="task_list_embed", description="タスク一覧をEmbedで表示（更新は常に一番下へ）。")
        @app_commands.describe(assignee="担当で絞り込み（任意）")
        @app_commands.choices(
            status=[
                app_commands.Choice(name="todo", value="todo"),
                app_commands.Choice(name="in_progress", value="in_progress"),
                app_commands.Choice(name="done", value="done"),
                app_commands.Choice(name="all", value="all"),
            ]
        )
        @app_commands.autocomplete(assignee=autocomplete_assignee)
        async def task_list_embed_cmd(
            interaction: discord.Interaction,
            status: Optional[app_commands.Choice[str]] = None,
            assignee: Optional[str] = None,
        ):
            channel = interaction.channel
            if not isinstance(channel, (discord.TextChannel, discord.Thread)):
                await interaction.response.send_message("対応チャンネルでのみ利用できます。", ephemeral=True)
                return

            await interaction.response.defer(ephemeral=True)
            issues = await self._collect_task_issues(channel, status, assignee)
            if not issues:
                await interaction.followup.send("該当なし。", ephemeral=True)
                return

            entries = [format_task_list_entry(issue) for issue in issues]
            title = f"タスク一覧（全{len(entries)}件）"
            message = await self._send_task_list_embed(channel, entries, title)
            await interaction.followup.send(f"最新一覧を再掲しました: [jump]({message.jump_url})", ephemeral=True)
    def define_presets(self):
        @self.tree.command(name="task_preset_save", description="プリセット保存（後で素早くグループ作成に使えます）。")
        @app_commands.describe(name="プリセット名", label_filters="ラベル（todo/doing/done, #bug など）", interval="推奨更新間隔(分)")
        @app_commands.autocomplete(label_filters=autocomplete_labels)
        async def task_preset_save_cmd(
            interaction: discord.Interaction,
            name: str,
            label_filters: Optional[str] = None,
            interval: Optional[app_commands.Range[int,1,180]] = None,
        ):
            await interaction.response.defer(ephemeral=True)
            iv = int(interval) if interval is not None else DEFAULT_INTERVAL_MIN
            filters = normalize_label_input(label_filters or "")
            await preset_save(name, filters, iv)
            await interaction.followup.send(f"保存: preset='{name}' interval={iv} labels={filters or 'なし'}", ephemeral=True)

        @self.tree.command(name="task_group_add_preset", description="保存済みプリセットからグループを追加します。")
        @app_commands.describe(name="プリセット名", group_name="新規グループ名", channel="対象チャンネル（省略時は現在）")
        @app_commands.autocomplete(name=autocomplete_preset_name)
        async def task_group_add_preset_cmd(interaction: discord.Interaction, name: str, group_name: str, channel: Optional[discord.TextChannel] = None):
            await interaction.response.defer(ephemeral=True)
            loaded = await preset_load(name)
            if not loaded:
                await interaction.followup.send("プリセットが見つかりません。", ephemeral=True); return
            labels, _ = loaded
            target_ch = channel or interaction.channel
            if not isinstance(target_ch, discord.TextChannel):
                await interaction.followup.send("テキストチャンネルを指定してください。", ephemeral=True); return
            bundle = await get_bundle(target_ch.id)
            if not bundle:
                content = await build_bundle_content(target_ch.id)
                msg = await target_ch.send(content=content)
                try: await msg.edit(suppress=True)
                except TypeError:
                    try: await msg.suppress_embeds(True)  # type: ignore[attr-defined]
                    except Exception: pass
                try: await msg.pin()
                except discord.Forbidden: pass
                await upsert_bundle(target_ch.id, msg.id, DEFAULT_INTERVAL_MIN, True, True)
            await upsert_bundle_group(target_ch.id, group_name, labels)
            self._bundle_last_refresh[target_ch.id] = 0
            await interaction.followup.send(f"OK: '{group_name}' を追加（preset='{name}'） labels={labels or 'なし'}", ephemeral=True)

    def define_admin_resync(self):
        @self.tree.command(name="admin_resync", description="（管理者）アプリコマンドを再同期します。ギルドにグローバル定義を反映。")
        async def admin_resync_cmd(interaction: discord.Interaction):
            if not interaction.user.guild_permissions.administrator:
                await interaction.response.send_message("権限不足。", ephemeral=True)
                return
            await interaction.response.defer(ephemeral=True)
            if not GUILD_ID:
                await interaction.followup.send("GUILD_ID 未設定。", ephemeral=True)
                return
            guild = discord.Object(id=GUILD_ID)
            self.tree.clear_commands(guild=guild)  # 同期関数
            self.tree.copy_global_to(guild=guild)
            diff = await self.tree.sync(guild=guild)
            cmds = await self.tree.fetch_commands(guild=guild)
            await interaction.followup.send(f"再同期: {len(cmds)} -> {[c.name for c in cmds]}（diff={len(diff)}）", ephemeral=True)

    # === セレクト＋ボタンUIを出すコマンド ===
    def define_task_groups_ui(self):
        @self.tree.command(name="task_groups_ui", description="対話UI（セレクト＋ボタン）でグループ管理を行います。")
        async def task_groups_ui_cmd(interaction: discord.Interaction):
            if not isinstance(interaction.channel, discord.TextChannel):
                await interaction.response.send_message("テキストチャンネルで実行してください。", ephemeral=True); return
            view = GroupsManageView(interaction.channel.id)
            await interaction.response.send_message("グループ管理UI", view=view, ephemeral=True)
            # 初期の選択肢をロード
            await view.refresh_options(interaction)

    # ---- 初回起動高速化: 一括定義→単発 sync ----
    async def setup_hook(self):
        # 一括登録（同期は最後に1回）
        registrars = [
            self.define_link_github,
            self.define_task_add,
            self.define_task_add_modal,
            self.define_task_unblock,
            self.define_task_reopen,
            self.define_task_assign,
            self.define_task_comment,
            self.define_task_search,
            self.define_task_status,
            self.define_task_claim,
            self.define_task_done,
            self.define_task_unclaim,
            self.define_task_bind_bundle,
            self.define_task_group_add,
            self.define_task_group_add_modal,
            self.define_task_unbind_tag,
            self.define_task_groups,
            self.define_task_groups_edit,
            self.define_task_groups_ui,   # UI
            self.define_presets,
            self.define_task_list,
            self.define_task_list_embed,
            self.define_admin_resync,
        ]
        for r in registrars:
            r()

        FORCE_CLEAR = os.getenv("COMMANDS_FORCE_CLEAR", "").lower() in ("1", "true", "yes")

        if GUILD_ID:
            guild = discord.Object(id=GUILD_ID)
            if FORCE_CLEAR:
                self.tree.clear_commands(guild=guild)
            self.tree.copy_global_to(guild=guild)
            diff = await self.tree.sync(guild=guild)
            cmds = await self.tree.fetch_commands(guild=guild)
            print(f"[SYNC] guild: diff={len(diff)} | cmds={len(cmds)} -> {[c.name for c in cmds]}")
        else:
            if FORCE_CLEAR:
                self.tree.clear_commands()
            diff = await self.tree.sync()
            cmds = await self.tree.fetch_commands()
            print(f"[SYNC] global: diff={len(diff)} | cmds={len(cmds)} -> {[c.name for c in cmds]}")

    # ===== 定期更新: バンドル単位（1分刻み） =====
    @tasks.loop(minutes=1)
    async def periodic_refresh(self):
        import time
        try:
            now = int(time.time())
            async with aiosqlite.connect(DB_PATH) as db:
                cur = await db.execute("SELECT channel_id, message_id, interval_min, pin, suppress FROM bundle")
                rows = await cur.fetchall()
            if not rows:
                return
            for ch_id, msg_id, iv, pin, sup in rows:
                last = self._bundle_last_refresh.get(int(ch_id), 0)
                if last and (now - last) < int(iv) * 60:
                    continue
                await refresh_bundle_message(self, int(ch_id), int(msg_id), bool(pin), bool(sup))
                self._bundle_last_refresh[int(ch_id)] = now
        except Exception as e:
            print("periodic_refresh error:", e)

    @periodic_refresh.before_loop
    async def before_periodic_refresh(self):
        await self.wait_until_ready()

# ===== バンドル更新 =====
async def refresh_bundle_message(client: discord.Client, channel_id: int, message_id: int, pin: bool, suppress: bool):
    channel = client.get_channel(channel_id)
    if not isinstance(channel, discord.TextChannel):
        return
    content = await build_bundle_content(channel_id)
    try:
        msg = await channel.fetch_message(message_id)
        try:
            await msg.edit(content=content, suppress=suppress)
        except TypeError:
            await msg.edit(content=content)
            if suppress:
                try:
                    await msg.suppress_embeds(True)  # type: ignore[attr-defined]
                except Exception:
                    pass
        if pin and not msg.pinned:
            try:
                await msg.pin()
            except discord.Forbidden:
                pass
        if not pin and msg.pinned:
            try:
                await msg.unpin()
            except discord.Forbidden:
                pass
    except discord.NotFound:
        # 消えていたら再作成
        new_msg = await channel.send(content=content)
        if suppress:
            try:
                await new_msg.edit(suppress=True)
            except TypeError:
                try:
                    await new_msg.suppress_embeds(True)  # type: ignore[attr-defined]
                except Exception:
                    pass
        if pin and not new_msg.pinned:
            try:
                await new_msg.pin()
            except discord.Forbidden:
                pass
        await upsert_bundle(channel_id, new_msg.id, DEFAULT_INTERVAL_MIN, pin, suppress)

# ========= エントリポイント =========
client = Bot()

@client.event
async def on_ready():
    await db_init()
    # 予温（非同期でオートコンプリート体感を改善）
    asyncio.create_task(get_repo_labels_cached())
    asyncio.create_task(get_repo_collaborators_cached())

    app = await client.application_info()
    print(f"Logged in as {client.user} (app_id={app.id})")
    client.periodic_refresh.start()

if __name__ == "__main__":
    if not DISCORD_TOKEN:
        raise RuntimeError("DISCORD_TOKEN 未設定")
    if not (GH_OWNER and GH_REPO):
        raise RuntimeError("GITHUB_OWNER/GITHUB_REPO 未設定")
    client.run(DISCORD_TOKEN)











