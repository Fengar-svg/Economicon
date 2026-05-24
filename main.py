import discord
from discord.ext import commands
import os
import json
import sqlite3
from pathlib import Path
from datetime import datetime
from dotenv import load_dotenv

# Token laden

load_dotenv()
TOKEN = os.getenv("TOKEN")
MAX_SELECT_OPTIONS = 25
MAX_EMBED_FIELD_VALUE = 1024

# Hilfsfunktionen für Pfade und Logging


def get_forum_lists_path():
    return Path(__file__).parent / "Rohdaten" / "forum_lists.json"


def get_memory_path():
    return Path(__file__).parent / "Rohdaten" / "bot_memory.json"


def get_log_path():
    return Path(__file__).parent / "Rohdaten" / "bot_log.txt"


def get_database_path():
    return Path(__file__).parent / "Rohdaten" / "economicon.sqlite3"


def default_memory():
    return {"tasks": [], "completed": []}


def log_action(action: str, details: str = ""):
    """Protokolliert Aktionen in die Log-Datei"""
    log_path = get_log_path()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_entry = f"[{timestamp}] {action}"
    if details:
        log_entry += f" | {details}"
    log_entry += "\n"

    with open(log_path, "a", encoding="utf-8") as f:
        f.write(log_entry)


def load_legacy_memory():
    path = get_memory_path()
    if path.exists():
        try:
            with open(path, "r", encoding="utf-8") as f:
                memory = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            log_action("MEMORY_LOAD_ERROR", str(e))
            return default_memory()

        if not isinstance(memory, dict):
            log_action("MEMORY_LOAD_ERROR", "Memory file is not a JSON object")
            return default_memory()

        memory.setdefault("tasks", [])
        memory.setdefault("completed", [])
        if not isinstance(memory["tasks"], list):
            memory["tasks"] = []
        if not isinstance(memory["completed"], list):
            memory["completed"] = []
        return memory
    return default_memory()


def load_legacy_forum_lists():
    path = get_forum_lists_path()
    if path.exists():
        try:
            with open(path, "r", encoding="utf-8") as f:
                lists = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            log_action("FORUM_LISTS_LOAD_ERROR", str(e))
            return {}

        if isinstance(lists, dict):
            return lists
        log_action("FORUM_LISTS_LOAD_ERROR",
                   "Forum lists file is not a JSON object")
    return {}


def connect_db():
    path = get_database_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    return sqlite3.connect(path)


def init_database():
    with connect_db() as db:
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS forum_lists (
                guild_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                posts_json TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (guild_id, name)
            )
            """
        )
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS guild_memory (
                guild_id INTEGER PRIMARY KEY,
                tasks_json TEXT NOT NULL,
                completed_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS app_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )


def migrate_legacy_data_for_guild(guild_id: int):
    meta_key = f"legacy_migrated:{guild_id}"
    with connect_db() as db:
        migrated = db.execute(
            "SELECT value FROM app_meta WHERE key = ?",
            (meta_key,),
        ).fetchone()
        if migrated:
            return

    legacy_lists = load_legacy_forum_lists()
    legacy_memory = load_legacy_memory()
    now = datetime.now().isoformat(timespec="seconds")

    with connect_db() as db:
        for name, posts in legacy_lists.items():
            if isinstance(name, str) and isinstance(posts, list):
                db.execute(
                    """
                    INSERT OR IGNORE INTO forum_lists
                    (guild_id, name, posts_json, updated_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (guild_id, name, json.dumps(posts, ensure_ascii=False), now),
                )

        existing_memory = db.execute(
            "SELECT guild_id FROM guild_memory WHERE guild_id = ?",
            (guild_id,),
        ).fetchone()
        if not existing_memory:
            db.execute(
                """
                INSERT INTO guild_memory
                (guild_id, tasks_json, completed_json, updated_at)
                VALUES (?, ?, ?, ?)
                """,
                (
                    guild_id,
                    json.dumps(legacy_memory["tasks"], ensure_ascii=False),
                    json.dumps(legacy_memory["completed"], ensure_ascii=False),
                    now,
                ),
            )

        db.execute(
            "INSERT OR REPLACE INTO app_meta (key, value) VALUES (?, ?)",
            (meta_key, now),
        )


def load_memory(guild_id: int):
    migrate_legacy_data_for_guild(guild_id)
    with connect_db() as db:
        row = db.execute(
            "SELECT tasks_json, completed_json FROM guild_memory WHERE guild_id = ?",
            (guild_id,),
        ).fetchone()

    if not row:
        return default_memory()

    try:
        tasks = json.loads(row[0])
        completed = json.loads(row[1])
    except json.JSONDecodeError as e:
        log_action("MEMORY_LOAD_ERROR", f"Guild: {guild_id}, Error: {str(e)}")
        return default_memory()

    return {
        "tasks": tasks if isinstance(tasks, list) else [],
        "completed": completed if isinstance(completed, list) else [],
    }


def save_memory(guild_id: int, memory):
    now = datetime.now().isoformat(timespec="seconds")
    with connect_db() as db:
        db.execute(
            """
            INSERT INTO guild_memory
            (guild_id, tasks_json, completed_json, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(guild_id) DO UPDATE SET
                tasks_json = excluded.tasks_json,
                completed_json = excluded.completed_json,
                updated_at = excluded.updated_at
            """,
            (
                guild_id,
                json.dumps(memory.get("tasks", []), ensure_ascii=False),
                json.dumps(memory.get("completed", []), ensure_ascii=False),
                now,
            ),
        )


def load_forum_lists(guild_id: int):
    migrate_legacy_data_for_guild(guild_id)
    lists = {}
    with connect_db() as db:
        rows = db.execute(
            "SELECT name, posts_json FROM forum_lists WHERE guild_id = ? ORDER BY name",
            (guild_id,),
        ).fetchall()

    for name, posts_json in rows:
        try:
            posts = json.loads(posts_json)
        except json.JSONDecodeError as e:
            log_action(
                "FORUM_LISTS_LOAD_ERROR",
                f"Guild: {guild_id}, List: {name}, Error: {str(e)}",
            )
            continue
        if isinstance(posts, list):
            lists[name] = posts
    return lists


def save_forum_list(guild_id: int, name: str, posts):
    now = datetime.now().isoformat(timespec="seconds")
    with connect_db() as db:
        db.execute(
            """
            INSERT INTO forum_lists
            (guild_id, name, posts_json, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(guild_id, name) DO UPDATE SET
                posts_json = excluded.posts_json,
                updated_at = excluded.updated_at
            """,
            (guild_id, name, json.dumps(posts, ensure_ascii=False), now),
        )


async def create_forum_post(forum: discord.ForumChannel, post_name: str):
    return await forum.create_thread(name=post_name, content=post_name[:2000])


def build_select_options(values):
    return [
        discord.SelectOption(label=value[:100], value=value)
        for value in list(values)[:MAX_SELECT_OPTIONS]
        if len(value) <= 100
    ]


def truncate_field_value(value: str) -> str:
    if len(value) <= MAX_EMBED_FIELD_VALUE:
        return value
    return value[: MAX_EMBED_FIELD_VALUE - 3] + "..."


# Bot Setup
intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True

bot = commands.Bot(command_prefix="Eco ", intents=intents)
init_database()

# Kategorien speichern (wird später aus der Datenbank kommen)
server_categories = {}

log_action("BOT_START", "Bot wird gestartet")


@bot.event
async def on_ready():
    for guild in bot.guilds:
        migrate_legacy_data_for_guild(guild.id)
    print(f"✓ Bot ist online als {bot.user}")
    await bot.tree.sync()
    print("✓ Commands synchronisiert")


@bot.command(name="create-forum-list")
async def create_forum_list_command(ctx, *, name: str = None):
    """Eco create-forum-list (Name) - Erstellt eine neue Forum-Post-Liste"""

    if not name:
        await ctx.send("❌ Bitte gib einen Namen für die Liste an: `Eco create-forum-list (Name)`")
        log_action("FORUM_LIST_COMMAND_ERROR", "No name provided")
        return
    if ctx.guild is None:
        await ctx.send("Dieser Befehl kann nur auf einem Server verwendet werden.")
        log_action("FORUM_LIST_COMMAND_ERROR", "Command used outside guild")
        return

    name = name.strip()
    if len(name) > 100:
        await ctx.send("Der Listenname darf maximal 100 Zeichen lang sein.")
        log_action("FORUM_LIST_COMMAND_ERROR", "Name too long")
        return

    log_action("FORUM_LIST_COMMAND_START",
               f"User: {ctx.author.id}, Name: {name}")

    modal = ForumListInputModal(ctx, name)
    await ctx.send(embed=discord.Embed(
        title="📋 Forum List Creator",
        description=f"Creating forum list: **{name}**\nPlease enter the post structure in the modal below.",
        color=discord.Color.blue()
    ))
    # Sende das Modal - da wir hier im Command sind, müssen wir anders vorgehen
    # Stattdessen speichern wir in einen Context
    ctx.bot.pending_forum_list = {
        "name": name, "user_id": ctx.author.id, "guild_id": ctx.guild.id}

    view = discord.ui.View()
    button = discord.ui.Button(label="Open Forum List Input",
                               style=discord.ButtonStyle.primary, custom_id="open_forum_list_modal")

    async def button_callback(interaction: discord.Interaction):
        if interaction.user.id != ctx.author.id:
            await interaction.response.send_message("❌ Du darfst das nicht benutzen!", ephemeral=True)
            log_action("FORUM_LIST_UNAUTHORIZED",
                       f"User: {interaction.user.id}")
            return
        modal = ForumListInputModal(interaction, name)
        await interaction.response.send_modal(modal)

    button.callback = button_callback
    view.add_item(button)

    await ctx.send("Klick den Button um die Forum-Liste zu erstellen:", view=view)


@bot.command(name="generate")
async def generate_command(ctx, *args):
    """Eco generate server-elements Befehl"""

    if ctx.guild is None:
        await ctx.send("Dieser Befehl kann nur auf einem Server verwendet werden.")
        log_action("GENERATE_ERROR", "Command used outside guild")
        return

    if not args or args[0] != "server-elements":
        await ctx.send("Ungültiger Befehl. Nutze: `Eco generate server-elements`")
        log_action("GENERATE_ERROR", "Ungültiger Befehl")
        return

    log_action("GENERATE_START",
               f"User: {ctx.author.id}, Guild: {ctx.guild.id}")

    # Kategorien vom Server laden
    guild_categories = {cat.name: cat.id for cat in ctx.guild.categories}
    server_categories[ctx.guild.id] = guild_categories

    # Erste Auswahl: New oder Existing
    embed = discord.Embed(
        title="🏗️ Server Elements Generator",
        description="Do you want to set a new list or edit an existing category?",
        color=discord.Color.blue()
    )

    view = FirstChoiceView(ctx)
    await ctx.send(embed=embed, view=view)


@bot.command(name="fix")
async def fix_command(ctx):
    """Eco fix - Überprüft fehlende Aufgaben und führt diese aus"""

    if ctx.guild is None:
        await ctx.send("Dieser Befehl kann nur auf einem Server verwendet werden.")
        log_action("FIX_ERROR", "Command used outside guild")
        return

    log_action("FIX_START", f"User: {ctx.author.id}, Guild: {ctx.guild.id}")
    guild_id = ctx.guild.id
    bot_memory = load_memory(guild_id)

    if not bot_memory["tasks"]:
        await ctx.send("✅ Keine fehlenden Aufgaben gefunden!")
        log_action("FIX_NO_TASKS", "Keine Aufgaben in der Memory")
        return

    embed = discord.Embed(
        title="🔧 Bot Repair",
        description=f"Überprüfe {len(bot_memory['tasks'])} fehlende Aufgaben...",
        color=discord.Color.orange()
    )
    status_msg = await ctx.send(embed=embed)

    try:
        guild = ctx.guild
        fixed_count = 0

        # Kopie um während der Iteration zu entfernen
        for task in bot_memory["tasks"][:]:
            try:
                if task["type"] == "forum_with_posts":
                    # Finde die Kategorie
                    category = discord.utils.get(
                        guild.categories, name=task["category"])
                    if not category:
                        category = discord.utils.get(
                            guild.categories, id=task.get("category_id"))

                    if not category:
                        log_action("FIX_CATEGORY_NOT_FOUND",
                                   f"Category: {task['category']}")
                        continue

                    # Überprüfe Foren
                    for forum_name in task["forums"]:
                        forum = discord.utils.get(
                            guild.forums, name=forum_name)

                        if not forum:
                            # Forum muss erstellt werden
                            forum = await guild.create_forum(name=forum_name, category=category)
                            log_action("FIX_FORUM_CREATED",
                                       f"Forum: {forum_name}")

                        # Überprüfe Threads/Posts
                        for post_name in task["posts"]:
                            existing_thread = discord.utils.get(
                                forum.threads, name=post_name)
                            if not existing_thread:
                                await create_forum_post(forum, post_name)
                                log_action(
                                    "FIX_THREAD_CREATED", f"Forum: {forum_name}, Thread: {post_name}")
                                fixed_count += 1

                elif task["type"] == "forum_free_form":
                    # Finde die Kategorie
                    category = discord.utils.get(
                        guild.categories, name=task["category"])
                    if not category:
                        category = discord.utils.get(
                            guild.categories, id=task.get("category_id"))

                    if not category:
                        log_action("FIX_CATEGORY_NOT_FOUND",
                                   f"Category: {task['category']}")
                        continue

                    # Überprüfe Foren (komplexe Struktur mit Posts)
                    for forum_name, child_posts in task["forums"]:
                        forum = discord.utils.get(
                            guild.forums, name=forum_name)

                        if not forum:
                            forum = await guild.create_forum(name=forum_name, category=category)
                            log_action("FIX_FORUM_CREATED",
                                       f"Forum: {forum_name}")

                        # Überprüfe Threads/Posts
                        for post_name in child_posts:
                            existing_thread = discord.utils.get(
                                forum.threads, name=post_name)
                            if not existing_thread:
                                await create_forum_post(forum, post_name)
                                log_action(
                                    "FIX_THREAD_CREATED", f"Forum: {forum_name}, Thread: {post_name}")
                                fixed_count += 1

                elif task["type"] == "channels":
                    # Überprüfe Kanäle
                    category = discord.utils.get(
                        guild.categories, name=task["category"])
                    if not category:
                        category = discord.utils.get(
                            guild.categories, id=task.get("category_id"))

                    if not category:
                        log_action("FIX_CATEGORY_NOT_FOUND",
                                   f"Category: {task['category']}")
                        continue

                    for channel_name in task["channels"]:
                        existing_channel = discord.utils.get(
                            guild.text_channels, name=channel_name)
                        if not existing_channel:
                            await guild.create_text_channel(name=channel_name, category=category)
                            log_action("FIX_CHANNEL_CREATED",
                                       f"Channel: {channel_name}")
                            fixed_count += 1

                # Markiere als abgeschlossen
                bot_memory["completed"].append(task)
                bot_memory["tasks"].remove(task)
                save_memory(guild_id, bot_memory)

            except Exception as e:
                log_action("FIX_TASK_ERROR", f"Task: {task}, Error: {str(e)}")
                continue

        result_embed = discord.Embed(
            title="✅ Reparatur abgeschlossen",
            description=f"**Reparierte Elemente:** {fixed_count}\n**Verbleibende Aufgaben:** {len(bot_memory['tasks'])}",
            color=discord.Color.green()
        )
        await status_msg.edit(embed=result_embed)
        log_action(
            "FIX_COMPLETE", f"Fixed: {fixed_count}, Remaining: {len(bot_memory['tasks'])}")

    except Exception as e:
        log_action("FIX_ERROR", str(e))
        await ctx.send(f"❌ Fehler beim Reparieren: {str(e)}")


class FirstChoiceView(discord.ui.View):
    def __init__(self, ctx):
        super().__init__(timeout=300)
        self.ctx = ctx
        self.category_name = None

    @discord.ui.select(
        placeholder="Choose an option...",
        options=[
            discord.SelectOption(label="New Category", value="new"),
            discord.SelectOption(label="Existing Category", value="existing")
        ]
    )
    async def first_choice(self, interaction: discord.Interaction, select: discord.ui.Select):
        await interaction.response.defer()

        if select.values[0] == "new":
            # Direkt zum nächsten Schritt für neue Kategorie
            embed = discord.Embed(
                title="🏗️ Server Elements Generator",
                description="Do you want to create channels or forums?",
                color=discord.Color.blue()
            )
            view = ChannelForumChoiceView(self.ctx, None)
            await interaction.followup.send(embed=embed, view=view)

        else:  # existing
            # Kategorien anzeigen
            guild_categories = server_categories.get(self.ctx.guild.id, {})
            if not guild_categories:
                await interaction.followup.send("❌ Keine Kategorien vorhanden. Erstelle erst eine neue Kategorie.")
                return

            options = build_select_options(guild_categories.keys())

            embed = discord.Embed(
                title="🏗️ Server Elements Generator",
                description="Choose a category:",
                color=discord.Color.blue()
            )

            view = CategorySelectView(self.ctx, options)
            await interaction.followup.send(embed=embed, view=view)


class CategorySelectView(discord.ui.View):
    def __init__(self, ctx, options):
        super().__init__(timeout=300)
        self.ctx = ctx

        # Select mit den korrekten Optionen erstellen
        select = discord.ui.Select(
            placeholder="Choose a category...",
            options=options,
            custom_id="category_select"
        )
        select.callback = self.category_select
        self.add_item(select)

    async def category_select(self, interaction: discord.Interaction):
        await interaction.response.defer()

        selected_category = interaction.data["values"][0]

        embed = discord.Embed(
            title="🏗️ Server Elements Generator",
            description="Do you want to create channels or forums?",
            color=discord.Color.blue()
        )
        view = ChannelForumChoiceView(self.ctx, selected_category)
        await interaction.followup.send(embed=embed, view=view)


class ChannelForumChoiceView(discord.ui.View):
    def __init__(self, ctx, category_name):
        super().__init__(timeout=300)
        self.ctx = ctx
        self.category_name = category_name

    @discord.ui.select(
        placeholder="Choose an option...",
        options=[
            discord.SelectOption(label="Channels", value="channels"),
            discord.SelectOption(label="Forums", value="forums")
        ]
    )
    async def channel_forum_choice(self, interaction: discord.Interaction, select: discord.ui.Select):
        choice = select.values[0]

        if choice == "channels":
            modal = ChannelInputModal(self.ctx, self.category_name)
            await interaction.response.send_modal(modal)
        else:  # forums
            # Zeige die Auswahl zwischen Post Lists und Free Form
            embed = discord.Embed(
                title="🏗️ Server Elements Generator",
                description="Choose forum creation mode:",
                color=discord.Color.blue()
            )
            view = ForumModeChoiceView(self.ctx, self.category_name)
            await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


class ForumModeChoiceView(discord.ui.View):
    def __init__(self, ctx, category_name):
        super().__init__(timeout=300)
        self.ctx = ctx
        self.category_name = category_name

    @discord.ui.select(
        placeholder="Choose forum mode...",
        options=[
            discord.SelectOption(label="Post Lists", value="post_lists"),
            discord.SelectOption(label="Free Form", value="free_form")
        ]
    )
    async def forum_mode_choice(self, interaction: discord.Interaction, select: discord.ui.Select):
        choice = select.values[0]

        if choice == "post_lists":
            forum_lists = load_forum_lists(interaction.guild.id)
            # Zeige verfügbare Listen
            if not forum_lists:
                await interaction.response.send_message(
                    "❌ Keine Forum-Listen vorhanden. Erstelle erst eine mit `Eco create-forum-list (Name)`",
                    ephemeral=True
                )
                return

            options = build_select_options(forum_lists.keys())
            if not options:
                await interaction.response.send_message(
                    "❌ Keine gültigen Forum-Listen gefunden. Listennamen dürfen maximal 100 Zeichen lang sein.",
                    ephemeral=True
                )
                return

            embed = discord.Embed(
                title="📋 Forum List Selection",
                description="Choose a post list:",
                color=discord.Color.blue()
            )

            view = PostListSelectionView(self.ctx, self.category_name, options)
            await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

        else:  # free_form
            modal = ForumInputModal(self.ctx, self.category_name)
            await interaction.response.send_modal(modal)


class PostListSelectionView(discord.ui.View):
    def __init__(self, ctx, category_name, options):
        super().__init__(timeout=300)
        self.ctx = ctx
        self.category_name = category_name

        select = discord.ui.Select(
            placeholder="Choose a post list...",
            options=options,
            custom_id="post_list_select"
        )
        select.callback = self.post_list_select
        self.add_item(select)

    async def post_list_select(self, interaction: discord.Interaction):
        selected_list = interaction.data["values"][0]

        embed = discord.Embed(
            title="🏗️ Server Elements Generator",
            description="Type in every forum in a new line.\nThe post list will be used for every forum.",
            color=discord.Color.blue()
        )

        modal = ForumWithPostListModal(
            self.ctx, self.category_name, selected_list)
        await interaction.response.send_modal(modal)


class ForumListInputModal(discord.ui.Modal):
    def __init__(self, interaction_or_ctx, name):
        super().__init__(title=f"Create Forum List: {name}", timeout=600)
        self.name = name

        if isinstance(interaction_or_ctx, discord.Interaction):
            self.ctx = interaction_or_ctx
        else:
            self.ctx = interaction_or_ctx

        self.post_input = discord.ui.TextInput(
            label="Post Structure",
            placeholder="One post per line",
            style=discord.TextStyle.paragraph,
            required=True
        )
        self.add_item(self.post_input)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer()

        posts = [post.strip()
                 for post in self.post_input.value.split("\n") if post.strip()]

        if not posts:
            await interaction.followup.send("❌ Keine Posts eingegeben!")
            log_action("FORUM_LIST_CREATE_EMPTY",
                       f"User: {interaction.user.id}, List: {self.name}")
            return

        guild_id = interaction.guild.id
        save_forum_list(guild_id, self.name, posts)
        log_action("FORUM_LIST_CREATED",
                   f"Name: {self.name}, Posts: {len(posts)}")

        embed = discord.Embed(
            title="✅ Forum-Liste erstellt",
            description=f"**Name:** {self.name}\n**Posts:**\n" +
            "\n".join([f"• {post}" for post in posts]),
            color=discord.Color.green()
        )
        await interaction.followup.send(embed=embed)


class ForumWithPostListModal(discord.ui.Modal):
    def __init__(self, ctx, category_name, post_list_name):
        super().__init__(title="Create Forums with Post List", timeout=600)
        self.ctx = ctx
        self.category_name = category_name
        self.post_list_name = post_list_name

        self.forum_input = discord.ui.TextInput(
            label="Forum Names",
            placeholder="One forum per line",
            style=discord.TextStyle.paragraph,
            required=True
        )
        self.add_item(self.forum_input)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer()

        forum_names = [name.strip()
                       for name in self.forum_input.value.split("\n") if name.strip()]

        if not forum_names:
            await interaction.followup.send("❌ Keine Forum-Namen eingegeben!")
            log_action("FORUM_WITH_LIST_EMPTY", f"User: {interaction.user.id}")
            return

        try:
            guild = interaction.guild
            guild_id = guild.id
            forum_lists = load_forum_lists(guild_id)
            post_structure = forum_lists.get(self.post_list_name, [])
            if not post_structure:
                await interaction.followup.send("❌ Die ausgewählte Forum-Liste wurde nicht gefunden oder ist leer.")
                log_action(
                    "FORUM_LIST_MISSING",
                    f"Guild: {guild_id}, List: {self.post_list_name}",
                )
                return

            bot_memory = load_memory(guild_id)
            category = None

            if self.category_name:
                category = discord.utils.get(
                    guild.categories, name=self.category_name)

            if not category:
                category = await guild.create_category(
                    name=self.category_name or "Generated Elements"
                )
                server_categories.setdefault(guild.id, {})[
                    category.name] = category.id
                log_action("CATEGORY_CREATED", f"Category: {category.name}")

            # Speichere die Task in der Memory für den Fix-Befehl
            task = {
                "type": "forum_with_posts",
                "category": category.name,
                "category_id": category.id,
                "forums": forum_names,
                "posts": post_structure,
                "post_list": self.post_list_name
            }
            bot_memory["tasks"].append(task)
            save_memory(guild_id, bot_memory)
            log_action("FORUM_LIST_TASK_STARTED",
                       f"List: {self.post_list_name}, Forums: {len(forum_names)}")

            created_forums = []
            has_errors = False
            for forum_name in forum_names:
                try:
                    forum = await guild.create_forum(
                        name=forum_name,
                        category=category
                    )

                    # Erstelle Posts in dem Forum
                    for post_name in post_structure:
                        try:
                            await create_forum_post(forum, post_name)
                            log_action(
                                "THREAD_CREATED", f"Forum: {forum_name}, Post: {post_name}")
                        except Exception as e:
                            has_errors = True
                            print(
                                f"Fehler beim Erstellen des Posts {post_name}: {e}")
                            log_action(
                                "THREAD_CREATE_ERROR", f"Forum: {forum_name}, Post: {post_name}, Error: {str(e)}")

                    forum_info = f"📌 **{forum.name}** ({len(post_structure)} Posts)"
                    created_forums.append(forum_info)
                    log_action(
                        "FORUM_CREATED", f"Forum: {forum_name}, Category: {category.name}, Posts: {len(post_structure)}")

                except Exception as e:
                    has_errors = True
                    print(f"Fehler beim Erstellen von {forum_name}: {e}")
                    log_action("FORUM_CREATE_ERROR",
                               f"Forum: {forum_name}, Error: {str(e)}")

            if not has_errors:
                # Entferne erfolgreich abgeschlossene Task
                bot_memory["tasks"].remove(task)
                bot_memory["completed"].append(task)
            save_memory(guild_id, bot_memory)

            embed = discord.Embed(
                title="✅ Foren erstellt",
                description=f"**Kategorie:** {category.name}\n**Post-Liste:** {self.post_list_name}\n\n" + "\n".join(
                    created_forums),
                color=discord.Color.green()
            )
            await interaction.followup.send(embed=embed)
            log_action("FORUM_LIST_SUCCESS",
                       f"Category: {category.name}, List: {self.post_list_name}, Forums: {len(forum_names)}")

        except Exception as e:
            embed = discord.Embed(
                title="❌ Fehler",
                description=f"Fehler beim Erstellen der Foren:\n{str(e)}",
                color=discord.Color.red()
            )
            await interaction.followup.send(embed=embed)
            log_action("FORUM_LIST_ERROR", str(e))


class ChannelInputModal(discord.ui.Modal):
    def __init__(self, ctx, category_name):
        super().__init__(title="Create Channels", timeout=600)
        self.ctx = ctx
        self.category_name = category_name

        self.channel_input = discord.ui.TextInput(
            label="Channel Names",
            placeholder="One channel name per line",
            style=discord.TextStyle.paragraph,
            required=True
        )
        self.add_item(self.channel_input)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer()

        channel_names = [
            name.strip() for name in self.channel_input.value.split("\n") if name.strip()]

        if not channel_names:
            await interaction.followup.send("❌ Keine Kanalnamen eingegeben!")
            log_action("CHANNEL_EMPTY", f"User: {interaction.user.id}")
            return

        try:
            guild = interaction.guild
            guild_id = guild.id
            bot_memory = load_memory(guild_id)
            category = None

            # Existierende Kategorie finden oder neue erstellen
            if self.category_name:
                category = discord.utils.get(
                    guild.categories, name=self.category_name)

            if not category:
                # Neue Kategorie erstellen
                category = await guild.create_category(
                    name=self.category_name or "Generated Elements"
                )
                server_categories.setdefault(guild.id, {})[
                    category.name] = category.id
                log_action("CATEGORY_CREATED", f"Category: {category.name}")

            # Speichere die Task in der Memory für den Fix-Befehl
            task = {
                "type": "channels",
                "category": category.name,
                "category_id": category.id,
                "channels": channel_names
            }
            bot_memory["tasks"].append(task)
            save_memory(guild_id, bot_memory)

            # Kanäle erstellen
            created_channels = []
            has_errors = False
            for channel_name in channel_names:
                try:
                    channel = await guild.create_text_channel(
                        name=channel_name,
                        category=category
                    )
                    created_channels.append(channel.mention)
                    log_action(
                        "CHANNEL_CREATED", f"Channel: {channel_name}, Category: {category.name}")
                except Exception as e:
                    has_errors = True
                    print(f"Fehler beim Erstellen von {channel_name}: {e}")
                    log_action("CHANNEL_CREATE_ERROR",
                               f"Channel: {channel_name}, Error: {str(e)}")

            if not has_errors:
                # Entferne erfolgreich abgeschlossene Task
                bot_memory["tasks"].remove(task)
                bot_memory["completed"].append(task)
            save_memory(guild_id, bot_memory)

            # Erfolgs-Nachricht
            embed = discord.Embed(
                title="✅ Kanäle erstellt",
                description=f"**Kategorie:** {category.name}\n\n**Kanäle:**\n" + "\n".join(
                    created_channels),
                color=discord.Color.green()
            )
            await interaction.followup.send(embed=embed)
            log_action(
                "CHANNEL_SUCCESS", f"Category: {category.name}, Channels: {len(channel_names)}")

        except Exception as e:
            embed = discord.Embed(
                title="❌ Fehler",
                description=f"Fehler beim Erstellen der Kanäle:\n{str(e)}",
                color=discord.Color.red()
            )
            await interaction.followup.send(embed=embed)
            log_action("CHANNEL_ERROR", str(e))


class ForumInputModal(discord.ui.Modal):
    def __init__(self, ctx, category_name):
        super().__init__(title="Create Forums", timeout=600)
        self.ctx = ctx
        self.category_name = category_name

        self.forum_input = discord.ui.TextInput(
            label="Forum Structure",
            placeholder="Forum names without space. Posts with leading space.",
            style=discord.TextStyle.paragraph,
            required=True
        )
        self.add_item(self.forum_input)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer()

        lines = self.forum_input.value.split("\n")
        forums_data = self.parse_forum_structure(lines)

        if not forums_data:
            await interaction.followup.send("❌ Keine Forum-Namen eingegeben!")
            log_action("FORUM_EMPTY", f"User: {interaction.user.id}")
            return

        try:
            guild = interaction.guild
            guild_id = guild.id
            bot_memory = load_memory(guild_id)
            category = None

            # Existierende Kategorie finden oder neue erstellen
            if self.category_name:
                category = discord.utils.get(
                    guild.categories, name=self.category_name)

            if not category:
                # Neue Kategorie erstellen
                category = await guild.create_category(
                    name=self.category_name or "Generated Elements"
                )
                server_categories.setdefault(guild.id, {})[
                    category.name] = category.id
                log_action("CATEGORY_CREATED", f"Category: {category.name}")

            # Extrahiere Forum-Namen und alle Posts
            forum_names = [f[0] for f in forums_data]
            all_posts = []
            for forum_name, child_posts in forums_data:
                all_posts.extend(child_posts)
            all_posts = list(set(all_posts))  # Eindeutige Posts

            # Speichere die Task in der Memory für den Fix-Befehl
            task = {
                "type": "forum_free_form",
                "category": category.name,
                "category_id": category.id,
                "forums": forums_data  # Speichere komplette Struktur
            }
            bot_memory["tasks"].append(task)
            save_memory(guild_id, bot_memory)

            # Foren erstellen
            created_forums = []
            has_errors = False
            for forum_name, child_posts in forums_data:
                try:
                    forum = await guild.create_forum(
                        name=forum_name,
                        category=category
                    )

                    # Erstelle Posts in dem Forum
                    for post_name in child_posts:
                        try:
                            await create_forum_post(forum, post_name)
                            log_action(
                                "THREAD_CREATED", f"Forum: {forum_name}, Post: {post_name}")
                        except Exception as e:
                            has_errors = True
                            print(
                                f"Fehler beim Erstellen des Posts {post_name}: {e}")
                            log_action(
                                "THREAD_CREATE_ERROR", f"Forum: {forum_name}, Post: {post_name}, Error: {str(e)}")

                    forum_info = f"📌 **{forum.name}**"
                    if child_posts:
                        forum_info += f" ({len(child_posts)} Posts)"
                    else:
                        forum_info += " (keine Posts)"

                    created_forums.append(forum_info)
                    log_action(
                        "FORUM_CREATED", f"Forum: {forum_name}, Category: {category.name}, Posts: {len(child_posts)}")

                except Exception as e:
                    has_errors = True
                    print(f"Fehler beim Erstellen von {forum_name}: {e}")
                    log_action("FORUM_CREATE_ERROR",
                               f"Forum: {forum_name}, Error: {str(e)}")

            if not has_errors:
                # Entferne erfolgreich abgeschlossene Task
                bot_memory["tasks"].remove(task)
                bot_memory["completed"].append(task)
            save_memory(guild_id, bot_memory)

            # Erfolgs-Nachricht
            embed = discord.Embed(
                title="✅ Foren erstellt",
                description=f"**Kategorie:** {category.name}\n\n" +
                "\n".join(created_forums),
                color=discord.Color.green()
            )
            await interaction.followup.send(embed=embed)
            log_action("FORUM_FREE_FORM_SUCCESS",
                       f"Category: {category.name}, Forums: {len(forum_names)}")

        except Exception as e:
            embed = discord.Embed(
                title="❌ Fehler",
                description=f"Fehler beim Erstellen der Foren:\n{str(e)}",
                color=discord.Color.red()
            )
            await interaction.followup.send(embed=embed)
            log_action("FORUM_FREE_FORM_ERROR", str(e))

    def parse_forum_structure(self, lines):
        """Parst die Forum-Struktur aus den Eingabezeilen"""
        forums = []
        current_forum = None

        for line in lines:
            if not line.strip():
                continue

            if line.startswith(" "):
                # Kind-Post
                if current_forum is not None:
                    post_name = line.strip()
                    current_forum[1].append(post_name)
            else:
                # Forum-Name
                forum_name = line.strip()
                if forum_name:
                    current_forum = [forum_name, []]
                    forums.append(current_forum)

        return forums


@bot.command(name="status")
async def status_command(ctx):
    """Eco status - Zeigt den Bot-Status und ausstehende Aufgaben"""

    if ctx.guild is None:
        await ctx.send("Dieser Befehl kann nur auf einem Server verwendet werden.")
        log_action("STATUS_ERROR", "Command used outside guild")
        return

    bot_memory = load_memory(ctx.guild.id)
    forum_lists = load_forum_lists(ctx.guild.id)
    pending = len(bot_memory["tasks"])
    completed = len(bot_memory["completed"])
    lists = len(forum_lists)

    embed = discord.Embed(
        title="📊 Bot Status",
        description="Aktueller Status des Economicon Bots",
        color=discord.Color.blue()
    )
    embed.add_field(name="⏳ Ausstehende Aufgaben",
                    value=str(pending), inline=True)
    embed.add_field(name="✅ Abgeschlossene Aufgaben",
                    value=str(completed), inline=True)
    embed.add_field(name="📋 Forum-Listen", value=str(lists), inline=True)

    if pending > 0:
        embed.add_field(
            name="⚠️ Hinweis", value="Es gibt noch Aufgaben! Verwende `Eco fix` um sie zu vervollständigen.", inline=False)

    if lists > 0:
        list_names = ", ".join(forum_lists.keys())
        embed.add_field(name="📋 Verfügbare Listen",
                        value=truncate_field_value(list_names), inline=False)

    await ctx.send(embed=embed)
    log_action("STATUS_COMMAND", f"User: {ctx.author.id}")


@bot.command(name="logs")
async def logs_command(ctx, limit: int = 10):
    """Eco logs [limit] - Zeigt die letzten Log-Einträge an"""

    log_path = get_log_path()

    if not log_path.exists():
        await ctx.send("❌ Keine Log-Datei gefunden.")
        return

    with open(log_path, "r", encoding="utf-8") as f:
        lines = f.readlines()

    # Nimm die letzten 'limit' Zeilen
    recent_logs = lines[-limit:]

    log_content = "".join(recent_logs)

    # Discord hat eine maximale Nachrichtenlänge von 2000 Zeichen
    if len(log_content) > 1900:
        log_content = log_content[-1900:]

    embed = discord.Embed(
        title=f"📋 Letzte {len(recent_logs)} Log-Einträge",
        description=f"```\n{log_content}\n```",
        color=discord.Color.blue()
    )

    await ctx.send(embed=embed)
    log_action("LOGS_COMMAND", f"User: {ctx.author.id}, Limit: {limit}")


# Bot starten
if not TOKEN:
    raise RuntimeError("TOKEN fehlt. Bitte setze TOKEN in der .env-Datei.")

bot.run(TOKEN)
