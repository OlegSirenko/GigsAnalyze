import sqlite3
import json
import re
import os
import itertools
from collections import defaultdict, Counter
from datetime import datetime
import html as html_mod
from pyvis.network import Network
import networkx as nx
from networkx.algorithms import community

# ==========================================
# 1. Настройки
# ==========================================
DB_PATH = 'posters_web.db'
BANDS_JSON = 'bands.json'
MANUAL_EDGES_JSON = 'manual_edges.json'
EXCLUSIONS_JSON = 'exclusions.json'
OUTPUT_HTML = os.environ.get('ANALYZE_OUTPUT_HTML', 'index.html')  # GitHub Pages / сайт

DEFAULT_USERNAME = 'GigsArchive'      # публичный username канала-архива
CHANNEL_USERNAMES = {}                # если каналов несколько: {'1234567890': 'GigsArchive'}

# ==========================================
# 2. Загрузка данных
# ==========================================
with open(BANDS_JSON, 'r', encoding='utf-8') as f:
    raw_bands = json.load(f)
bands_dict = {k.strip(): [a.strip() for a in v if a.strip()] for k, v in raw_bands.items() if k.strip()}

manual_edges = []
if os.path.exists(MANUAL_EDGES_JSON):
    with open(MANUAL_EDGES_JSON, 'r', encoding='utf-8') as f:
        manual_edges = json.load(f)
    # страховка от пробелов в ключах и значениях ("source ", "D.I.C.E " и т.п.)
    manual_edges = [
        {k.strip(): (v.strip() if isinstance(v, str) else v) for k, v in e.items()}
        for e in manual_edges
    ]
    print(f"Загружено ручных связей: {len(manual_edges)}")


def parse_exclusion(entry):
    """Понимает: https://t.me/GigsArchive/1221, https://t.me/c/123456/1221,
    123456/1221, просто 1221. Возвращает (канал или None, msg_id)."""
    entry = str(entry).strip()
    m = re.search(r't\.me/(?:c/)?([^/]+)/(\d+)', entry)
    if m:
        return m.group(1), m.group(2)
    m = re.fullmatch(r'([^/\s]+)/(\d+)', entry)
    if m:
        return m.group(1), m.group(2)
    if entry.isdigit():
        return None, entry
    return None, None


exclusions = {}  # группа -> {(канал или None, msg_id)}
if os.path.exists(EXCLUSIONS_JSON):
    with open(EXCLUSIONS_JSON, 'r', encoding='utf-8') as f:
        raw_exclusions = json.load(f)
    for band, entries in raw_exclusions.items():
        for entry in entries:
            ch, mid = parse_exclusion(entry)
            if mid is None:
                print(f"⚠️ Не распознано исключение для {band.strip()}: {entry}")
                continue
            exclusions.setdefault(band.strip(), set()).add((ch, mid))

total_exclusions = sum(len(v) for v in exclusions.values())
print(f"Загружено исключений: {total_exclusions}")


def strip_html(text):
    if not text:
        return ""
    return html_mod.unescape(re.sub(r'<[^>]+>', ' ', text))


def is_excluded(band, chat_str, msg_str):
    for ch, mid in exclusions.get(band, ()):
        if mid != msg_str:
            continue
        if ch is None or ch == chat_str or ch == CHANNEL_USERNAMES.get(chat_str):
            return True
        if not ch.isdigit() and not CHANNEL_USERNAMES:
            return True  # канал задан username'ом, карты нет — сверяем по ID поста
    return False


# ==========================================
# 3. Сбор связей и упоминаний из БД
# ==========================================
print("Анализирую базу данных...")
band_mentions = Counter()
edge_metadata = defaultdict(list)
node_metadata = defaultdict(list)
exclusion_hits = 0

with sqlite3.connect(DB_PATH) as conn:
    cursor = conn.cursor()
    query = """
        SELECT id, caption, event_date, channel_chat_id, channel_message_id
        FROM web_posters
        WHERE COALESCE(is_cancelled, 0) = 0
    """
    rows = cursor.execute(query).fetchall()

    for row in rows:
        post_id, caption, event_date, chat_id, msg_id = row
        if not caption:
            continue

        clean_post = strip_html(caption)
        found_bands = []

        date_str = "Дата не указана"
        if event_date:
            try:
                dt = datetime.strptime(str(event_date)[:10], '%Y-%m-%d')
                date_str = dt.strftime('%d.%m.%Y')
            except ValueError:
                pass

        # Публичная ссылка вида https://t.me/GigsArchive/1380
        link = ""
        short_link = ""
        chat_str = ""
        msg_str = str(msg_id) if msg_id else ""

        if chat_id and msg_id:
            chat_str = str(chat_id)
            if chat_str.startswith('-100'):
                chat_str = chat_str[4:]
            elif chat_str.startswith('-'):
                chat_str = chat_str[1:]
            short_link = f"{chat_str}/{msg_id}"
            username = CHANNEL_USERNAMES.get(chat_str) or DEFAULT_USERNAME
            link = f"https://t.me/{username}/{msg_id}"

        for canonical, aliases in bands_dict.items():
            # Исключение: группа не учитывается в этом посте вообще
            if msg_str and is_excluded(canonical, chat_str, msg_str):
                exclusion_hits += 1
                continue

            search_terms = [canonical] + aliases
            for term in search_terms:
                if not term:
                    continue
                if re.search(r'(?<![\w])' + re.escape(term) + r'(?![\w])', clean_post, re.IGNORECASE):
                    found_bands.append(canonical)
                    band_mentions[canonical] += 1
                    node_metadata[canonical].append({'date': date_str, 'link': link})
                    break

        unique_bands = list(set(found_bands))
        if len(unique_bands) >= 2:
            for b1, b2 in itertools.combinations(sorted(unique_bands), 2):
                edge_metadata[(b1, b2)].append({'date': date_str, 'link': link})

# ==========================================
# 4. Добавление ручных связей
# ==========================================
for edge in manual_edges:
    b1, b2 = sorted([edge['source'], edge['target']])
    date_str = edge.get('date', 'Вручную')
    link = edge.get('link', '')
    note = edge.get('note', '')

    # исключения действуют и на ручные связи
    ch, mid = parse_exclusion(link)
    if mid and (is_excluded(b1, ch or '', mid) or is_excluded(b2, ch or '', mid)):
        exclusion_hits += 1
        continue

    edge_metadata[(b1, b2)].append({'date': date_str, 'link': link, 'note': note})
    if b1 not in band_mentions:
        band_mentions[b1] = 0
    if b2 not in band_mentions:
        band_mentions[b2] = 0

        # не дублируем событие, если этот же пост уже дал упоминание группы
    def already_has(band):
        return any(m.get('link') == link for m in node_metadata.get(band, ()))

    if not already_has(b1):
        node_metadata[b1].append({'date': date_str, 'link': link, 'note': f"Вручную с {b2}"})
    if not already_has(b2):
        node_metadata[b2].append({'date': date_str, 'link': link, 'note': f"Вручную с {b1}"})

# ==========================================
# 5. Граф и кластеры (NetworkX)
# ==========================================
G_nx = nx.Graph()
for (b1, b2), occurrences in edge_metadata.items():
    G_nx.add_edge(b1, b2, weight=len(occurrences))

if G_nx.number_of_edges() > 0:
    communities = list(community.greedy_modularity_communities(G_nx))
    band_to_community = {}
    for i, comm in enumerate(communities):
        for band in comm:
            band_to_community[band] = i
    print(f"🎨 Найдено кластеров (тусовок): {len(communities)}")
else:
    band_to_community = {}

# ==========================================
# 6. Настройка PyVis
# ==========================================
net = Network(height='100vh', width='100%', bgcolor='#0a0a0f', font_color='#f5efdc', directed=False)
net.barnes_hut(gravity=-12000, central_gravity=0.3, spring_length=300, spring_strength=0.04, damping=0.09)

net.set_options("""
var options = {
  "physics": {
    "stabilization": {
      "enabled": true,
      "iterations": 300,
      "fit": true
    }
  }
}
""")

# Палитра в стиле сайта Gigs Archive (gold / red / purple / green на тёмном)
colors = ['#F4D03F', '#E74C3C', '#9B59B6', '#27AE60', '#F5EFDC',
          '#E67E22', '#3498DB', '#1ABC9C', '#E84393', '#A855F7',
          '#FF6B4A', '#FFCA3A', '#45B7D1', '#D65DB1', '#96CEB4',
          '#FFD54F', '#4ECDC4', '#7986CB', '#AED581']

# ==========================================
# 7. Узлы
# ==========================================
added_nodes = set()
for band, count in band_mentions.items():
    size = min(10 + (max(count, 1) * 4), 60)
    comm_id = band_to_community.get(band, -1)

    if comm_id != -1:
        color = colors[comm_id % len(colors)]
        title = f"{band}\nУпоминаний: {count}\nКлан: #{comm_id + 1}"
    else:
        color = '#7c7869'
        title = f"{band}\nУпоминаний: {count}\nОдиночка"

    net.add_node(band, label=band, value=size, color=color, title=title, font={'size': 14})
    added_nodes.add(band)

# ==========================================
# 8. Связи
# ==========================================
edges_added = 0
for (b1, b2), occurrences in edge_metadata.items():
    if b1 in added_nodes and b2 in added_nodes:
        weight = len(occurrences)
        edge_width = 1 + (weight * 1.5)
        opacity = min(0.2 + (weight * 0.15), 0.9)

        def sort_key(x):
            return datetime.strptime(x['date'], '%d.%m.%Y') if x['date'] not in ('Вручную', 'Дата не указана') else datetime.min
        sorted_occ = sorted(occurrences, key=sort_key)

        dates_list = [occ['date'] for occ in sorted_occ]
        tooltip = f"{b1} + {b2}\n\n" + "\n".join(dates_list)

        net.add_edge(b1, b2, value=weight, title=tooltip,
                     color={'color': '#F4D03F', 'opacity': opacity}, width=edge_width)
        edges_added += 1

# ==========================================
# 9. Сохранение и ВНЕДРЕНИЕ JS
# ==========================================
temp_file = "temp_network.html"
net.save_graph(temp_file)

with open(temp_file, 'r', encoding='utf-8') as f:
    html_content = f.read()
os.remove(temp_file)

# viewport для мобильных (pyvis его не добавляет) + фирменные шрифты сайта
html_content = html_content.replace(
    '</head>',
    '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
    '<link rel="preconnect" href="https://fonts.googleapis.com">\n'
    '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>\n'
    '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Anton&family=Courier+Prime&family=Oswald:wght@700&family=Rubik+Mono+One&family=Special+Elite&display=swap">\n'
    '</head>'
)

js_node_data = json.dumps(dict(node_metadata), ensure_ascii=False)
js_edge_data = json.dumps({f"{b1}|{b2}": v for (b1, b2), v in edge_metadata.items()}, ensure_ascii=False)

custom_js = f"""
<style>
    /* ===== Стиль в палитре сайта Gigs Archive (ransom / grunge) ===== */
    :root {{
        --ink: #0A0A0F;
        --gold: #F4D03F;
        --red: #E74C3C;
        --purple: #9B59B6;
        --green: #27AE60;
        --paper: #f5efdc;
        --muted: #7c7869;
        --border: rgba(244, 208, 63, .22);
        --font-type: 'Special Elite', 'Courier Prime', 'PT Mono', 'Courier New', monospace;
        --font-stencil: 'Rubik Mono One', 'Oswald', Impact, 'Arial Black', sans-serif;
    }}
    html, body {{ margin: 0; height: 100%; }}
    body {{
        overflow: hidden;
        font-family: var(--font-type);
        color: var(--paper);
        background:
            radial-gradient(ellipse at 20% 0%, rgba(155, 89, 182, .10), transparent 45%),
            radial-gradient(ellipse at 85% 100%, rgba(231, 76, 60, .08), transparent 45%),
            var(--ink);
    }}
    #mynetwork, #mynetwork canvas, .vis-network {{ background: var(--ink) !important; background-color: var(--ink) !important; }}
    div.vis-network div.vis-manipulation, .vis-tooltip {{
        background: var(--ink) !important; color: var(--paper) !important;
        border: 2px solid var(--border) !important;
    }}

    /* --- Верхняя панель в стиле сайта --- */
    #topbar {{
        position: fixed; top: 0; left: 0; right: 0; height: 56px; z-index: 1100;
        display: flex; align-items: center; justify-content: space-between;
        padding: 0 16px; box-sizing: border-box;
        background: rgba(10, 10, 15, .92); backdrop-filter: blur(4px);
        border-bottom: 3px double var(--gold);
    }}
    #topbar .tb-brand {{
        font-family: var(--font-stencil); font-size: 1rem; letter-spacing: .06em;
        color: var(--gold); text-decoration: none; text-shadow: 2px 2px 0 rgba(0,0,0,.6);
    }}
    #topbar nav {{ display: flex; gap: 18px; }}
    #topbar nav a {{
        color: var(--paper); text-decoration: none; font-size: .95rem; font-weight: 700;
        padding: 4px 8px; border: 2px solid transparent; transition: all .15s ease;
    }}
    #topbar nav a:hover {{ color: var(--ink); background: var(--gold); transform: rotate(-1deg); }}
    #topbar nav a.active {{ color: var(--gold); border-color: var(--gold); }}

    #left-panel {{
        position: fixed; top: 68px; left: 10px; width: 280px; max-height: 80vh;
        overflow-y: auto; background: rgba(20, 20, 29, .96); color: var(--paper);
        padding: 15px; border: 2px solid var(--border); border-radius: 2px;
        z-index: 1000; font-family: var(--font-type);
        box-shadow: 5px 6px 0 rgba(0, 0, 0, .55);
    }}
    #left-panel h2 {{
        margin: 0 0 12px; font-family: var(--font-stencil); font-size: 1rem;
        color: var(--gold); letter-spacing: .05em; text-transform: uppercase;
    }}
    #search-input {{
        width: 100%; padding: 10px; margin-bottom: 15px; border-radius: 2px;
        border: 2px solid var(--border); background: var(--ink); color: var(--paper);
        box-sizing: border-box; font-size: 14px; font-family: var(--font-type);
    }}
    #search-input::placeholder {{ color: var(--muted); }}
    #search-input:focus {{ outline: none; border-color: var(--gold); }}
    #band-list {{ list-style: none; padding: 0; margin: 0; }}
    #band-list li {{
        padding: 10px; cursor: pointer; border-bottom: 1px dashed rgba(244, 208, 63, .15);
        font-size: 14px; transition: background .15s, color .15s;
        display: flex; justify-content: space-between; gap: 8px;
    }}
    #band-list li:hover {{ background: var(--gold); color: var(--ink); }}
    .band-count {{ color: var(--muted); font-size: 12px; }}
    #band-list li:hover .band-count {{ color: var(--ink); }}

    #info-panel {{
        position: fixed; top: 68px; right: 10px; width: 350px; max-height: 80vh;
        overflow-y: auto; background: rgba(20, 20, 29, .96); color: var(--paper);
        padding: 20px; border: 2px solid var(--border); border-radius: 2px;
        display: none; z-index: 1000; font-family: var(--font-type);
        box-shadow: 5px 6px 0 rgba(0, 0, 0, .55);
    }}
    #info-panel h3 {{
        margin-top: 0; color: var(--gold); font-family: var(--font-stencil);
        border-bottom: 2px dashed var(--border); padding-bottom: 10px;
    }}
    #info-panel ul {{ padding-left: 20px; margin: 0; }}
    #info-panel li {{ margin-bottom: 8px; line-height: 1.4; }}
    #info-panel a {{ color: var(--purple); text-decoration: none; font-weight: bold; margin-left: 5px; }}
    #info-panel a:hover {{ color: var(--red); text-decoration: underline; }}
    #close-panel {{ float: right; cursor: pointer; color: var(--muted); font-size: 20px; margin-top: -5px; }}
    #close-panel:hover {{ color: var(--gold); }}
    .subtitle {{ font-size: 12px; color: var(--muted); margin-bottom: 15px; display: block; }}

    #panel-toggle {{
        display: none; position: fixed; top: 68px; left: 10px; z-index: 1001;
        background: var(--gold); color: var(--ink); border: 2px solid var(--ink);
        border-radius: 2px; padding: 10px 14px; font-size: 15px; cursor: pointer;
        font-family: var(--font-stencil); box-shadow: 3px 3px 0 rgba(0, 0, 0, .6);
    }}

    @media (max-width: 768px) {{
        #topbar nav {{ gap: 8px; }}
        #topbar nav a {{ font-size: .78rem; padding: 3px 5px; }}
        #panel-toggle {{ display: block; }}
        #left-panel {{
            display: none;
            top: 118px; left: 10px; right: 10px; width: auto;
            max-height: 45vh;
        }}
        #left-panel.open {{ display: block; }}
        #info-panel {{
            top: auto; bottom: 10px; left: 10px; right: 10px; width: auto;
            max-height: 50vh;
        }}
        #search-input {{ font-size: 16px; }}
        #band-list li {{ padding: 12px 10px; font-size: 15px; }}
        #close-panel {{ font-size: 26px; padding: 0 6px; }}
    }}
</style>

<div id="topbar">
    <a class="tb-brand" href="/">GIGS ARCHIVE</a>
    <nav>
        <a href="/">Главная</a>
        <a href="/posters">Афиши</a>
        <a href="/articles">Статьи</a>
        <a href="/network" class="active">Граф</a>
        <a href="https://t.me/{DEFAULT_USERNAME}" target="_blank">Telegram</a>
    </nav>
</div>

<button id="panel-toggle" onclick="document.getElementById('left-panel').classList.toggle('open')">☰ Группы</button>

<div id="left-panel">
    <h2>Группы</h2>
    <input type="text" id="search-input" placeholder="🔍 Поиск группы...">
    <ul id="band-list"></ul>
</div>

<div id="info-panel">
    <span id="close-panel" onclick="document.getElementById('info-panel').style.display='none';">✖</span>
    <h3 id="panel-title">Детали</h3>
    <span class="subtitle" id="panel-subtitle"></span>
    <ul id="panel-content"></ul>
</div>

<script type="text/javascript">
    const nodeData = {js_node_data};
    const edgeData = {js_edge_data};
    const allBands = Object.keys(nodeData).sort((a, b) => nodeData[b].length - nodeData[a].length);

    function renderList(items) {{
        if (items.length === 0) return "<li>Нет постов</li>";
        let html = "";
        items.forEach(function(item) {{
            html += "<li>📅 " + item.date;
            if (item.note) html += " <i>(" + item.note + ")</i>";
            if (item.link) html += "<a href='" + item.link + "' target='_blank'>[Пост]</a>";
            html += "</li>";
        }});
        return html;
    }}

    function showNodeInfo(nodeId) {{
        if (window.innerWidth <= 768) document.getElementById('left-panel').classList.remove('open');
        var items = nodeData[nodeId] || [];
        document.getElementById('panel-title').innerText = "🎸 " + nodeId;
        document.getElementById('panel-subtitle').innerText = "Всего постов: " + items.length;
        document.getElementById('panel-content').innerHTML = renderList(items);
        document.getElementById('info-panel').style.display = 'block';
    }}

    function showEdgeInfo(edgeId) {{
        if (window.innerWidth <= 768) document.getElementById('left-panel').classList.remove('open');
        var edge = edges.get(edgeId);
        var key1 = edge.from + "|" + edge.to;
        var key2 = edge.to + "|" + edge.from;
        var items = edgeData[key1] || edgeData[key2] || [];

        document.getElementById('panel-title').innerText = "🤝 " + edge.from + " + " + edge.to;
        document.getElementById('panel-subtitle').innerText = "Играли вместе: " + items.length + " раз(а)";
        document.getElementById('panel-content').innerHTML = renderList(items);
        document.getElementById('info-panel').style.display = 'block';
    }}

    const listContainer = document.getElementById('band-list');
    function renderBandList(filterText = "") {{
        listContainer.innerHTML = "";
        const filtered = allBands.filter(b => b.toLowerCase().includes(filterText.toLowerCase()));

        if (filtered.length === 0) {{
            listContainer.innerHTML = "<li style='color: #888;'>Ничего не найдено</li>";
            return;
        }}

        filtered.forEach(band => {{
            const li = document.createElement('li');
            li.innerHTML = `<span>${{band}}</span> <span class="band-count">${{nodeData[band].length}}</span>`;
            li.onclick = () => {{
                if (typeof network !== 'undefined') {{
                    network.focus(band, {{ scale: 1.5, animation: {{ duration: 500, easingFunction: 'easeInOutQuad' }} }});
                    network.selectNodes([band]);
                    showNodeInfo(band);
                }}
            }};
            listContainer.appendChild(li);
        }});
    }}

    document.getElementById('search-input').addEventListener('input', function(e) {{
        renderBandList(e.target.value);
    }});

    setTimeout(function() {{
        if (typeof network !== 'undefined') {{
            renderBandList();

            network.on("click", function (params) {{
                if (params.nodes.length > 0) {{
                    showNodeInfo(params.nodes[0]);
                }} else if (params.edges.length > 0) {{
                    showEdgeInfo(params.edges[0]);
                }} else {{
                    document.getElementById('info-panel').style.display = 'none';
                }}
            }});
        }}
    }}, 1000);
</script>
</body>
"""

html_content = html_content.replace("</body>", custom_js)

with open(OUTPUT_HTML, 'w', encoding='utf-8') as f:
    f.write(html_content)

print(f"\n✅ Граф успешно создан!")
print(f"📊 Групп (узлов): {len(added_nodes)}")
print(f"🔗 Связей (линий): {edges_added}")
print(f"🚫 Применено исключений: {exclusion_hits}")
print(f"📁 Откройте файл '{OUTPUT_HTML}' в вашем браузере.")