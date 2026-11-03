import asyncio
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.getenv('DISCORD_TOKEN')
ALLOWED_ROLES = {'с РанЧо', '!Человек с метлой!'}
DB = Path(__file__).with_name('ranch.db')
INITIAL = [
    ('Пшено',773,650),('Сахарный тростник',513,500),('Сахар',294,500),
    ('Мука',205,200),('Тысячелистник',650,650),('Женьшень Аляскинский',502,500),
    ('Женьшень Американский',507,500),('Алоэ',539,500),('Прерийный Мак',239,300),
    ('Английская булава',180,300),('Чок дейзи',165,500),('Зимняя ягода',334,500),
    ('Шпинат',246,300),('Душица',612,300),('Картошка',162,300),
    ('Ползучий тимьян',265,300),('Дикая морковь',220,300),
    ('Мягкая древесина',200,100),('Твердая древесина',37,100),
]

conn = sqlite3.connect(DB)
conn.execute('PRAGMA journal_mode=WAL')
conn.execute('CREATE TABLE IF NOT EXISTS products (name TEXT PRIMARY KEY, amount INTEGER NOT NULL CHECK(amount >= 0), target INTEGER NOT NULL CHECK(target > 0))')
conn.execute('CREATE TABLE IF NOT EXISTS settings (guild_id INTEGER PRIMARY KEY, stock_channel INTEGER NOT NULL, stock_message INTEGER NOT NULL, log_channel INTEGER NOT NULL)')
conn.execute('CREATE TABLE IF NOT EXISTS operations (id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER, user_id INTEGER, product TEXT, delta INTEGER, before_amount INTEGER, after_amount INTEGER, created_at TEXT)')
if conn.execute('SELECT COUNT(*) FROM products').fetchone()[0] == 0:
    conn.executemany('INSERT INTO products VALUES (?, ?, ?)', INITIAL)
conn.commit()
lock = asyncio.Lock()
intents = discord.Intents.default()
bot = commands.Bot(command_prefix='!', intents=intents)


def allowed(member):
    return isinstance(member, discord.Member) and (member.guild_permissions.administrator or any(role.name in ALLOWED_ROLES for role in member.roles))


def status(amount, target):
    fraction = amount / target
    return '🔴' if fraction < 0.5 else '🟡' if fraction < 0.9 else '🟢'


def stock_embed():
    rows = conn.execute('SELECT name, amount, target FROM products').fetchall()
    rows.sort(key=lambda r: (r[1] / r[2], r[0].casefold()))
    counts = {'🔴': 0, '🟡': 0, '🟢': 0}
    lines = []
    for name, amount, target in rows:
        icon = status(amount, target)
        counts[icon] += 1
        lines.append(f'{icon} **{name}** — {amount}/{target} — **+{max(target - amount, 0)}**')
    embed = discord.Embed(title='🌾 ЗАПАСЫ РАНЧО', description='**Продукт — запас — нужно пополнить**\n\n' + '\n'.join(lines), color=discord.Color.green())
    embed.add_field(name='📊 Состояние запасов', value=f'🔴 Критические: **{counts["🔴"]}**  ·  🟡 Пополнить: **{counts["🟡"]}**  ·  🟢 В норме: **{counts["🟢"]}**', inline=False)
    embed.set_footer(text=f'Всего позиций: {len(rows)} · Обновлено (UTC)')
    embed.timestamp = datetime.now(timezone.utc)
    return embed


async def refresh(guild):
    setting = conn.execute('SELECT stock_channel, stock_message FROM settings WHERE guild_id=?', (guild.id,)).fetchone()
    if not setting:
        return
    channel = guild.get_channel(setting[0]) or await bot.fetch_channel(setting[0])
    message = await channel.fetch_message(setting[1])
    await message.edit(embed=stock_embed(), view=StockView())


async def record_log(guild, user, product, delta, before, after):
    setting = conn.execute('SELECT log_channel FROM settings WHERE guild_id=?', (guild.id,)).fetchone()
    if not setting:
        return
    channel = guild.get_channel(setting[0]) or await bot.fetch_channel(setting[0])
    action = '📥 Положил(а)' if delta > 0 else '📤 Взял(а)'
    await channel.send(f'{action}: {user.mention}\n**{product}**: {delta:+d} шт. · Было: {before} · Стало: **{after}**')


async def apply_change(interaction, product, delta):
    if not allowed(interaction.user):
        await interaction.response.send_message('⛔ У вас нет нужной роли.', ephemeral=True)
        return
    async with lock:
        row = conn.execute('SELECT amount FROM products WHERE name=?', (product,)).fetchone()
        if row is None:
            await interaction.response.send_message('Продукт не найден.', ephemeral=True)
            return
        before = row[0]
        after = before + delta
        if after < 0:
            await interaction.response.send_message(f'Недостаточно запасов: доступно {before} шт.', ephemeral=True)
            return
        conn.execute('UPDATE products SET amount=? WHERE name=?', (after, product))
        conn.execute('INSERT INTO operations (guild_id,user_id,product,delta,before_amount,after_amount,created_at) VALUES (?,?,?,?,?,?,?)', (interaction.guild_id, interaction.user.id, product, delta, before, after, datetime.now(timezone.utc).isoformat()))
        conn.commit()
        await interaction.response.send_message(f'✅ {product}: {delta:+d} шт. Новый остаток: **{after}**.', ephemeral=True)
        try:
            await refresh(interaction.guild)
        except (discord.HTTPException, discord.Forbidden) as exc:
            print('Не удалось обновить сообщение:', exc)
        try:
            await record_log(interaction.guild, interaction.user, product, delta, before, after)
        except (discord.HTTPException, discord.Forbidden) as exc:
            print('Не удалось записать историю:', exc)


class QuantityModal(discord.ui.Modal):
    quantity = discord.ui.TextInput(label='Количество (целое число больше нуля)', placeholder='Например, 50', max_length=9)

    def __init__(self, product, sign):
        super().__init__(title=('Положить: ' if sign > 0 else 'Взять: ') + product[:30])
        self.product = product
        self.sign = sign

    async def on_submit(self, interaction):
        raw = str(self.quantity.value).strip()
        if not raw.isascii() or not raw.isdecimal() or int(raw) <= 0:
            await interaction.response.send_message('Введите положительное целое число.', ephemeral=True)
            return
        await apply_change(interaction, self.product, self.sign * int(raw))


class ProductSelect(discord.ui.Select):
    def __init__(self, sign):
        self.sign = sign
        names = [row[0] for row in conn.execute('SELECT name FROM products ORDER BY name COLLATE NOCASE')]
        super().__init__(placeholder='Выберите продукт', options=[discord.SelectOption(label=n, value=n) for n in names[:25]])

    async def callback(self, interaction):
        if not allowed(interaction.user):
            await interaction.response.send_message('⛔ У вас нет нужной роли.', ephemeral=True)
            return
        await interaction.response.send_modal(QuantityModal(self.values[0], self.sign))


class SelectView(discord.ui.View):
    def __init__(self, sign):
        super().__init__(timeout=180)
        self.add_item(ProductSelect(sign))


class StockView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label='Взять', style=discord.ButtonStyle.danger, emoji='📤', custom_id='ranch:take')
    async def take(self, interaction, button):
        if not allowed(interaction.user):
            await interaction.response.send_message('⛔ У вас нет нужной роли.', ephemeral=True)
            return
        await interaction.response.send_message('Выберите, что взять:', view=SelectView(-1), ephemeral=True)

    @discord.ui.button(label='Положить', style=discord.ButtonStyle.success, emoji='📥', custom_id='ranch:put')
    async def put(self, interaction, button):
        if not allowed(interaction.user):
            await interaction.response.send_message('⛔ У вас нет нужной роли.', ephemeral=True)
            return
        await interaction.response.send_message('Выберите, что положить:', view=SelectView(1), ephemeral=True)


@bot.event
async def on_ready():
    print(f'Бот подключён: {bot.user}')


@bot.event
async def setup_hook():
    bot.add_view(StockView())

    guild = discord.Object(id=1554839201017757768)
    bot.tree.copy_global_to(guild=guild)
    synced = await bot.tree.sync(guild=guild)
    print(f'Команды зарегистрированы: {len(synced)}')


def admin_only(interaction):
    return (
        isinstance(interaction.user, discord.Member)
        and (
            interaction.user.guild_permissions.administrator
            or any(role.name == '!Человек с метлой!' for role in interaction.user.roles)
        )
    )


@bot.tree.command(name='запустить_запасы', description='Создать или обновить главное сообщение запасов')
@app_commands.guild_only()
@app_commands.check(admin_only)
@app_commands.describe(канал='Канал с запасами', история='Канал с историей операций')
async def start_stock(interaction: discord.Interaction, канал: discord.TextChannel, история: discord.TextChannel):
    await interaction.response.defer(ephemeral=True)
    async with lock:
        setting = conn.execute('SELECT stock_channel,stock_message FROM settings WHERE guild_id=?', (interaction.guild_id,)).fetchone()
        message = None
        if setting and setting[0] == канал.id:
            try:
                message = await канал.fetch_message(setting[1])
            except discord.NotFound:
                pass
        if message is None:
            message = await канал.send(embed=stock_embed(), view=StockView())
        else:
            await message.edit(embed=stock_embed(), view=StockView())
        conn.execute('INSERT OR REPLACE INTO settings VALUES (?,?,?,?)', (interaction.guild_id, канал.id, message.id, история.id))
        conn.commit()
    await interaction.followup.send(f'✅ Запасы опубликованы в {канал.mention}. История: {история.mention}', ephemeral=True)


@bot.tree.command(name='норма', description='Изменить норму продукта (администратор)')
@app_commands.guild_only()
@app_commands.check(admin_only)
async def target_cmd(interaction: discord.Interaction, продукт: str, количество: app_commands.Range[int, 1, 100000000]):
    async with lock:
        if not conn.execute('SELECT 1 FROM products WHERE name=?', (продукт,)).fetchone():
            await interaction.response.send_message('Продукт не найден. Проверьте название.', ephemeral=True)
            return
        conn.execute('UPDATE products SET target=? WHERE name=?', (количество, продукт))
        conn.commit()
        await interaction.response.send_message(f'✅ Новая норма «{продукт}»: {количество}.', ephemeral=True)
        await refresh(interaction.guild)


@bot.tree.command(name='остаток', description='Исправить текущий остаток (администратор)')
@app_commands.guild_only()
@app_commands.check(admin_only)
async def amount_cmd(interaction: discord.Interaction, продукт: str, количество: app_commands.Range[int, 0, 100000000]):
    async with lock:
        row = conn.execute('SELECT amount FROM products WHERE name=?', (продукт,)).fetchone()
        if row is None:
            await interaction.response.send_message('Продукт не найден. Проверьте название.', ephemeral=True)
            return
        before = row[0]
        conn.execute('UPDATE products SET amount=? WHERE name=?', (количество, продукт))
        conn.execute('INSERT INTO operations (guild_id,user_id,product,delta,before_amount,after_amount,created_at) VALUES (?,?,?,?,?,?,?)', (interaction.guild_id, interaction.user.id, продукт, количество-before, before, количество, datetime.now(timezone.utc).isoformat()))
        conn.commit()
        await interaction.response.send_message(f'✅ Остаток «{продукт}»: {количество}.', ephemeral=True)
        await refresh(interaction.guild)
        await record_log(interaction.guild, interaction.user, продукт, количество-before, before, количество)


@bot.tree.command(name='добавить_продукт', description='Добавить новый продукт (администратор)')
@app_commands.guild_only()
@app_commands.check(admin_only)
async def add_cmd(interaction: discord.Interaction, название: str, остаток: app_commands.Range[int, 0, 100000000], норма: app_commands.Range[int, 1, 100000000]):
    async with lock:
        if conn.execute('SELECT COUNT(*) FROM products').fetchone()[0] >= 25:
            await interaction.response.send_message('Достигнут лимит 25 продуктов для одного меню.', ephemeral=True)
            return
        try:
            conn.execute('INSERT INTO products VALUES (?,?,?)', (название, остаток, норма))
            conn.commit()
        except sqlite3.IntegrityError:
            await interaction.response.send_message('Продукт с таким названием уже существует.', ephemeral=True)
            return
        await interaction.response.send_message(f'✅ Добавлен продукт «{название}».', ephemeral=True)
        await refresh(interaction.guild)


@bot.tree.error
async def command_error(interaction, error):
    if isinstance(error, app_commands.CheckFailure):
        msg = '⛔ Эта команда доступна только администраторам сервера.'
    else:
        print('Ошибка команды:', repr(error))
        msg = 'Не удалось выполнить команду. Проверьте настройки и права бота.'
    if interaction.response.is_done():
        await interaction.followup.send(msg, ephemeral=True)
    else:
        await interaction.response.send_message(msg, ephemeral=True)


if __name__ == '__main__':
    if not TOKEN:
        raise SystemExit('Добавьте DISCORD_TOKEN в файл .env')
    bot.run(TOKEN)
