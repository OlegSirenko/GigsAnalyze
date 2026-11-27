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
OUTPUT_HTML = 'underground_network_full.html'

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
    if not text: return ""
    return html_mod.unescape(re.sub(r'<[^>]+>', ' ', text))

# ==========================================
# 3. Сбор связей и упоминаний из БД
# ==========================================
print("Анализирую базу данных...")
band_mentions = Counter()
edge_metadata = defaultdict(list)
node_metadata = defaultdict(list)

CHANNEL_USERNAMES = {}  # необязательно: {'1234567890': 'GigsArchive'}
exclusion_hits = 0

def is_excluded(band, chat_str, msg_str):
    for ch, mid in exclusions.get(band, ()):
        if mid != msg_str:
            continue
        if ch is None or ch == chat_str or ch == CHANNEL_USERNAMES.get(chat_str):
            return True
        if not ch.isdigit() and not CHANNEL_USERNAMES:
            return True  # канал задан username'ом, карты нет — сверяем по ID поста
    return False

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
        if not caption: continue
        
        clean_post = strip_html(caption)
        found_bands = []
        
        date_str = "Дата не указана"
        if event_date:
            try:
                dt = datetime.strptime(str(event_date)[:10], '%Y-%m-%d')
                date_str = dt.strftime('%d.%m.%Y')
            except:
                pass
                
        # Формируем все возможные форматы ссылок для проверки исключений
        link = ""
        short_link = ""
        chat_str = "GigsArchive"   # ← добавить
        msg_str = str(msg_id) if msg_id else ""
        
        if chat_id and msg_id:
            chat_str = str(chat_id)
            if chat_str.startswith('-100'):
                chat_str = chat_str[4:]
            elif chat_str.startswith('-'):
                chat_str = chat_str[1:]
            username = "GigsArchive"
            if username:
                link = f"https://t.me/{username}/{msg_id}"      # публичная: t.me/GigsArchive/1380
            else:
                link = f"https://t.me/c/{chat_str}/{msg_id}"    # запасной вариант
                short_link = f"{chat_str}/{msg_id}"
            
        for canonical, aliases in bands_dict.items():
            # ПРОВЕРКА ИСКЛЮЧЕНИЙ (ищем ID поста, полную ссылку, короткую или msg_id)
            if msg_str and is_excluded(canonical, chat_str, msg_str):
                exclusion_hits += 1
                continue
                    
            search_terms = [canonical] + aliases
            for term in search_terms:
                if not term: continue
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
    
    edge_metadata[(b1, b2)].append({'date': date_str, 'link': link, 'note': note})
    if b1 not in band_mentions: band_mentions[b1] = 0
    if b2 not in band_mentions: band_mentions[b2] = 0
    
    node_metadata[b1].append({'date': date_str, 'link': link, 'note': f"Вручную с {b2}"})
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
net = Network(height='900px', width='100%', bgcolor='#121212', font_color='white', directed=False)
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

colors = ['#FF595E', '#FFCA3A', '#8AC926', '#1982C4', '#6A4C93', 
          '#FF924C', '#45B7D1', '#96CEB4', '#FFEEAD', '#D65DB1',
          '#FF6B6B', '#4ECDC4', '#FFA07A', '#98D8C8',
          '#F06292', '#AED581', '#FFD54F', '#4DB6AC', '#7986CB']

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
        color = '#888888'
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
                     color={'color': '#ffffff', 'opacity': opacity}, width=edge_width)
        edges_added += 1

# ==========================================
# 9. Сохранение и ВНЕДРЕНИЕ JS
# ==========================================
temp_file = "temp_network.html"
net.save_graph(temp_file)

with open(temp_file, 'r', encoding='utf-8') as f:
    html_content = f.read()
os.remove(temp_file)

js_node_data = json.dumps({k: v for k, v in node_metadata.items()}, ensure_ascii=False)
js_edge_data = json.dumps({f"{b1}|{b2}": v for (b1, b2), v in edge_metadata.items()}, ensure_ascii=False)

custom_js = f"""
<style>
    #left-panel {{
        position: fixed; top: 10px; left: 10px; width: 280px; max-height: 90vh;
        overflow-y: auto; background: rgba(30, 30, 36, 0.95); color: white;
        padding: 15px; border-radius: 12px; border: 1px solid #444;
        z-index: 1000; font-family: 'Segoe UI', sans-serif;
        box-shadow: 0 4px 15px rgba(0,0,0,0.5);
    }}
    #search-input {{
        width: 100%; padding: 10px; margin-bottom: 15px; border-radius: 6px;
        border: 1px solid #555; background: #1e1e24; color: white; box-sizing: border-box;
        font-size: 14px;
    }}
    #band-list {{
        list-style: none; padding: 0; margin: 0;
    }}
    #band-list li {{
        padding: 10px; cursor: pointer; border-bottom: 1px solid #333;
        font-size: 14px; transition: background 0.2s; display: flex; justify-content: space-between;
    }}
    #band-list li:hover {{
        background: #444;
    }}
    .band-count {{
        color: #888; font-size: 12px;
    }}

    #info-panel {{
        position: fixed; top: 10px; right: 10px; width: 350px; max-height: 90vh;
        overflow-y: auto; background: rgba(30, 30, 36, 0.95); color: white;
        padding: 20px; border-radius: 12px; border: 1px solid #444;
        display: none; z-index: 1000; font-family: 'Segoe UI', sans-serif;
        box-shadow: 0 4px 15px rgba(0,0,0,0.5);
    }}
    #info-panel h3 {{ margin-top: 0; color: #ff6b6b; border-bottom: 1px solid #444; padding-bottom: 10px; }}
    #info-panel ul {{ padding-left: 20px; margin: 0; }}
    #info-panel li {{ margin-bottom: 8px; line-height: 1.4; }}
    #info-panel a {{ color: #4ECDC4; text-decoration: none; font-weight: bold; margin-left: 5px; }}
    #info-panel a:hover {{ text-decoration: underline; }}
    #close-panel {{ float: right; cursor: pointer; color: #aaa; font-size: 20px; margin-top: -5px; }}
    #close-panel:hover {{ color: white; }}
    .subtitle {{ font-size: 12px; color: #aaa; margin-bottom: 15px; display: block; }}
</style>

<div id="left-panel">
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
        var items = nodeData[nodeId] || [];
        document.getElementById('panel-title').innerText = "🎸 " + nodeId;
        document.getElementById('panel-subtitle').innerText = "Всего постов: " + items.length;
        document.getElementById('panel-content').innerHTML = renderList(items);
        document.getElementById('info-panel').style.display = 'block';
    }}

    function showEdgeInfo(edgeId) {{
        var edge = edges.get(edgeId);
        var key1 = edge.from + "|" + edge.to;
        var key2 = edge.to + "|" + edge.from;
        var items = edgeData[key1] || edgeData[key2] || [];
        
        document.getElementById('panel-title').innerText = edge.from + " + " + edge.to;
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
print(f"📁 Откройте файл '{OUTPUT_HTML}' в вашем браузере.")