# -*- coding: utf-8 -*-
"""CK3 Переводчик модов.

Находит в модах Crusader Kings III текст без русской локализации, переводит его
машинным переводчиком и кладёт перевод внутрь каждого мода:
    <мод>/localization/russian/zz_autoperevod_l_russian.yml
Чужие файлы не изменяются. Чтобы убрать перевод, достаточно удалить этот файл
(или запустить программу с ключом --clean).

Переведённое хранится в cache.json и повторно не переводится.

Запуск без параметров открывает окно. Параметры командной строки:
    --run     перевести без окна (настройки из config.json)
    --dry     только отчёт: сколько строк не переведено, ничего не писать
    --clean   удалить файлы zz_autoperevod из всех модов
"""
import json, re, sys, time, threading, urllib.parse, urllib.request, urllib.error
from pathlib import Path

HERE = Path(sys.executable if getattr(sys, "frozen", False) else __file__).resolve().parent
CONFIG = HERE / "config.json"
CACHE = HERE / "cache.json"       # английский -> русский; для других пар языков свой файл
SKIP = HERE / "skip.txt"
TRANSLATIONS = HERE / "translations"
OUT_PREFIX = "zz_autoperevod_l_"

# язык CK3: (код Google, код DeepL, название для нейросети)
LANGS = {
    "english": ("en", "EN", "английский"),
    "russian": ("ru", "RU", "русский"),
    "french": ("fr", "FR", "французский"),
    "german": ("de", "DE", "немецкий"),
    "spanish": ("es", "ES", "испанский"),
    "polish": ("pl", "PL", "польский"),
    "korean": ("ko", "KO", "корейский"),
    "simp_chinese": ("zh-CN", "ZH", "китайский (упрощённый)"),
    "japanese": ("ja", "JA", "японский"),
}


def cache_path(cfg):
    if (cfg["source_lang"], cfg["target_lang"]) == ("english", "russian"):
        return CACHE
    return HERE / f'cache_{cfg["source_lang"]}_{cfg["target_lang"]}.json'

DEFAULTS = {
    "mods_dir": str(Path.home() / "Documents" / "Paradox Interactive" / "Crusader Kings III" / "mod"),
    "workshop_dir": r"C:\Program Files (x86)\Steam\steamapps\workshop\content\1158310",
    "game_dir": r"C:\Program Files (x86)\Steam\steamapps\common\Crusader Kings III\game",
    "source_lang": "english",      # с какого языка переводить
    "target_lang": "russian",      # на какой язык
    "engine": "google_free",       # google_free | google_cloud | deepl | openrouter | silkcompute | custom
    "google_cloud_key": "",
    "deepl_key": "",
    "openrouter_key": "",
    "openrouter_model": "google/gemini-2.5-flash",
    "silk_key": "",
    "silk_model": "",
    "silk_base": "https://api.silkcompute.com/v1",
    "custom_base": "",             # любой другой сервис с API в формате OpenAI
    "custom_key": "",
    "custom_model": "",
    "retranslate": False,          # перевести заново, не брать машинный перевод из кэша
    "style": "",                   # пожелания к стилю для нейросети, например: "обращение на ты"
    "create_missing_keys": True,
    "only_enabled": True,          # только моды из текущего плейсета (dlc_load.json)
    "skip_mods": [],               # моды, которые не трогать (часть названия)
}
DEFAULT_SKIP = "# Регулярные выражения: ключи, которые не надо переводить (по одному в строке)\n# Пример: ^my_mod_secret_event\n"

LINE = re.compile(r'^\s*([^\s:#"]+):\d*\s*(".*")')
HEADER = re.compile(r'^\ufeff?\s*l_(\w+)\s*:')


class Stop(Exception):
    pass


# ---------------------------------------------------------------- файлы

def read_text(p):
    try:
        raw = p.read_bytes()
    except OSError:
        return ""
    for enc in ("utf-8-sig", "cp1251", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            pass
    return ""


def parse_yml(p):
    lang, keys = None, {}
    for line in read_text(p).splitlines():
        if lang is None:
            m = HEADER.match(line)
            if m:
                lang = m.group(1)
            continue
        m = LINE.match(line)
        if not m:
            continue
        val = m.group(2)
        end = val.rfind('"')
        cut = re.search(r'"\s+#[^"]*$', val)
        if cut:
            end = cut.start()
        keys[m.group(1)] = val[1:end]
    return lang, keys


def loc_keys(root, lang, skip_own=True):
    keys = {}
    d = root / "localization"
    if d.is_dir():
        for p in sorted(d.rglob("*.yml")):
            if not p.name.lower().endswith(f"_l_{lang}.yml"):
                continue
            if skip_own and p.name.startswith(OUT_PREFIX):
                continue
            l, k = parse_yml(p)
            if l == lang:
                keys.update(k)
    return keys


def find_mods(cfg):
    mods = []
    for base in (cfg["mods_dir"], cfg["workshop_dir"]):
        b = Path(base)
        if not base or not b.is_dir():
            continue
        for d in sorted(b.iterdir()):
            if d.is_dir() and (d / "descriptor.mod").is_file():
                n = re.search(r'^\s*name\s*=\s*"([^"]+)"', read_text(d / "descriptor.mod"), re.M)
                mods.append((n.group(1) if n else d.name, d))
    return mods


def enabled_paths(cfg):
    """Папки модов из текущего плейсета (его пишет лаунчер в dlc_load.json)."""
    docs = Path(cfg["mods_dir"]).parent
    load = load_json(docs / "dlc_load.json", None)
    if load is None:
        return None
    out = set()
    for rel in load.get("enabled_mods", []):
        m = re.search(r'^\s*path\s*=\s*"([^"]+)"', read_text(docs / rel), re.M)
        if m:
            p = Path(m.group(1))
            out.add((p if p.is_absolute() else docs / p).resolve())
    return out


# ------------------------------------------------- ключи без текста вовсе

BLOCK = re.compile(r'^([A-Za-z_][\w\.\-]*)\s*=\s*\{', re.M)
# папка -> какие ключи локализации нужны объекту
NEEDS = {
    "decisions": ["{k}", "{k}_desc", "{k}_tooltip", "{k}_confirm"],
    "character_interactions": ["{k}", "{k}_desc"],
    "traits": ["trait_{k}", "trait_{k}_desc"],
}
GENERIC = {"_desc": "", "_tooltip": "", "_confirm": "Confirm"}


def humanize(key):
    s = re.sub(r'^(trait_|rule_)', '', key)
    s = re.sub(r'_(decision|interaction|modifier|opinion)$', '', s)
    s = re.sub(r'^[a-z]{2,4}_(?=[a-z])', '', s) if s.count("_") >= 2 else s
    s = s.replace("_", " ").strip()
    return s[:1].upper() + s[1:]


def missing_objects(root, known):
    """Объекты скриптов, у которых нет текста ни на одном языке -> {ключ: английская заготовка}"""
    out = {}
    for folder, patterns in NEEDS.items():
        d = root / "common" / folder
        if not d.is_dir():
            continue
        for p in d.rglob("*.txt"):
            text = re.sub(r'#.*', '', read_text(p))
            for m in BLOCK.finditer(text):
                k = m.group(1)
                name_key = patterns[0].format(k=k)
                if name_key in known:
                    continue
                out[name_key] = humanize(k)
                for pat in patterns[1:]:
                    kk = pat.format(k=k)
                    suffix = kk[len(name_key):]
                    if kk not in known and suffix == "_confirm":
                        out[kk] = GENERIC[suffix]
    return out


# ---------------------------------------------------------- защита разметки

TOKEN = re.compile(
    r'\[[^\[\]]*\]'            # [recipient.GetName], [gold|E]
    r'|\$[^$\s]+\$'            # $OTHER_KEY$
    r'|#[A-Za-z_][\w;:]*!?\s?' # #X , #weak , #bold
    r'|#!'                     # конец формата
    r'|@[\w]+!'                # @warning_icon!
    r'|\\n|\\"|"'              # перенос строки и кавычки
    r'|£[\w]+£?'
)


def protect(text):
    tokens = []

    def repl(m):
        tokens.append(m.group(0))
        return "{%d}" % (len(tokens) - 1)
    return TOKEN.sub(repl, text), tokens


def restore(text, tokens):
    found = re.findall(r'\{\s*(\d+)\s*\}', text)
    if sorted(found) != sorted(str(i) for i in range(len(tokens))):
        return None
    out = re.sub(r'\{\s*(\d+)\s*\}', lambda m: tokens[int(m.group(1))], text)
    return tidy(out)


def tidy(s):
    s = re.sub(r'(?<!\\)"', "'", s) if False else s
    s = re.sub(r'\s+(\\n)', r'\1', s)
    s = re.sub(r'(\\n)[ \t]+', r'\1', s)
    s = re.sub(r' +([,.;:!?])', r'\1', s)
    s = re.sub(r'  +', ' ', s)
    return s.strip()


def has_text(s):
    return re.search(r'[^\W\d_]', TOKEN.sub(" ", s)) is not None


# ----------------------------------------------------------------- движки

def http(url, data=None, headers=None):
    req = urllib.request.Request(url, data=data, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        hint = {401: " Проверьте ключ API.", 402: " На счёте сервиса закончились средства.",
                404: " Проверьте название модели.", 429: " Слишком много запросов, попробуйте позже."}.get(e.code, "")
        detail = e.read().decode("utf-8", "replace")[:200]
        raise Stop(f"Сервис перевода ответил ошибкой {e.code}.{hint} {detail} Перевод остановлен, сделанное сохранено.")
    except OSError as e:
        raise Stop(f"Нет связи с сервисом перевода: {e}")


def google_free(lines, cfg):
    q = "\n".join(lines)
    url = "https://translate.googleapis.com/translate_a/single?" + urllib.parse.urlencode(
        {"client": "gtx", "sl": LANGS[cfg["source_lang"]][0], "tl": LANGS[cfg["target_lang"]][0], "dt": "t", "q": q})
    body = http(url)
    if not body.lstrip().startswith("["):
        raise Stop("Google отказал в автоматических запросах с этого адреса. Попробуйте позже "
                   "или укажите в config.json ключ google_cloud_key либо deepl_key.")
    text = "".join(seg[0] for seg in json.loads(body)[0] if seg and seg[0])
    return text.split("\n")


def google_cloud(lines, cfg):
    url = "https://translation.googleapis.com/language/translate/v2?key=" + urllib.parse.quote(cfg["google_cloud_key"])
    data = json.dumps({"q": lines, "source": LANGS[cfg["source_lang"]][0], "target": LANGS[cfg["target_lang"]][0], "format": "text"}).encode()
    body = json.loads(http(url, data, {"Content-Type": "application/json"}))
    return [t["translatedText"] for t in body["data"]["translations"]]


def deepl(lines, cfg):
    key = cfg["deepl_key"]
    host = "api-free.deepl.com" if key.endswith(":fx") else "api.deepl.com"
    data = json.dumps({"text": lines, "source_lang": LANGS[cfg["source_lang"]][1], "target_lang": LANGS[cfg["target_lang"]][1]}).encode()
    body = json.loads(http(f"https://{host}/v2/translate", data,
                           {"Content-Type": "application/json", "Authorization": "DeepL-Auth-Key " + key}))
    return [t["text"] for t in body["translations"]]


PROMPT = (
    "Ты переводчик модов для игры Crusader Kings III. Исходный язык: {src}. Язык перевода: {tgt}. "
    "На входе JSON-массив строк. Верни только JSON-массив той же длины с переводами в том же порядке, без пояснений. "
    "Метки вида {0}, {1} — это игровая разметка: сохрани каждую метку ровно один раз и поставь её по смыслу фразы. "
    "Пиши естественным литературным языком в духе средневековой стратегии "
    "и используй термины, принятые в официальной локализации CK3 на языке перевода. "
    "Короткие названия кнопок и решений переводи кратко. Не бери всю строку в кавычки."
)


def llm(base, key, model, lines, cfg):
    if not key or not model:
        raise Stop("Для этого переводчика укажите ключ API и название модели.")
    system = PROMPT.replace("{src}", LANGS[cfg["source_lang"]][2]).replace("{tgt}", LANGS[cfg["target_lang"]][2]) + (" Дополнительно: " + cfg["style"] if cfg.get("style") else "")
    data = json.dumps({
        "model": model, "temperature": 0.2,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": json.dumps(lines, ensure_ascii=False)}],
    }).encode()
    body = json.loads(http(base.rstrip("/") + "/chat/completions", data,
                           {"Content-Type": "application/json", "Authorization": "Bearer " + key,
                            "X-Title": "CK3 Mod Translator"}))
    try:
        text = body["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError):
        raise Stop("Сервис вернул неожиданный ответ: " + json.dumps(body, ensure_ascii=False)[:300])
    a, b = text.find("["), text.rfind("]")
    try:
        out = json.loads(text[a:b + 1])
    except ValueError:
        return []                              # вызывающий код повторит по одной строке
    return [str(x) for x in out] if isinstance(out, list) else []


def openrouter(lines, cfg):
    return llm("https://openrouter.ai/api/v1", cfg["openrouter_key"], cfg["openrouter_model"], lines, cfg)


def silkcompute(lines, cfg):
    return llm(cfg["silk_base"], cfg["silk_key"], cfg["silk_model"], lines, cfg)


def custom(lines, cfg):
    if not cfg.get("custom_base"):
        raise Stop("Укажите адрес API (custom_base) в config.json.")
    return llm(cfg["custom_base"], cfg["custom_key"], cfg["custom_model"], lines, cfg)


ENGINES = {"google_free": google_free, "google_cloud": google_cloud, "deepl": deepl,
           "openrouter": openrouter, "silkcompute": silkcompute, "custom": custom}
LLM_ENGINES = {"openrouter", "silkcompute", "custom"}


def translate_all(texts, cfg, cache, log):
    """Дополняет cache переводами для texts. Строки с разметкой защищаются плейсхолдерами."""
    engine = ENGINES[cfg["engine"]]
    again = cfg.get("retranslate")
    todo = [t for t in dict.fromkeys(texts) if (again or t not in cache) and has_text(t)]
    log(f"К переводу: {len(todo)} строк (в кэше уже {len(cache)})")
    batch, size, done = [], 0, 0

    def call(lines):
        res = engine(lines, cfg)
        if cfg["engine"] not in LLM_ENGINES:
            time.sleep(0.4)
        return res

    def flush():
        nonlocal batch, size, done
        if not batch:
            return
        prot = [protect(t) for t in batch]
        res = call([p[0] for p in prot])
        if len(res) != len(batch):          # сервис склеил строки: по одной
            res = [(call([p[0]]) or [""])[0] for p in prot]
        for src, (ptext, tokens), tr in zip(batch, prot, res):
            out = restore(tr, tokens)
            if out is None:                 # плейсхолдеры повреждены: переводим куски между ними
                parts = re.split(r'(\{\d+\})', ptext)
                idx = [i for i, p in enumerate(parts) if not re.fullmatch(r'\{\d+\}', p) and re.search(r'[^\W\d_]', p)]
                if idx:
                    got = call([parts[i].strip() for i in idx])
                    if len(got) == len(idx):
                        for i, g in zip(idx, got):
                            lead = parts[i][:len(parts[i]) - len(parts[i].lstrip())]
                            tail = parts[i][len(parts[i].rstrip()):]
                            parts[i] = lead + g + tail
                out = restore("".join(parts), tokens) or src
            cache[src] = out
        done += len(batch)
        batch, size = [], 0
        save_json(cache_path(cfg), cache)
        log(f"  переведено {done} из {len(todo)}")

    try:
        for t in todo:
            if size + len(t) > 3500 or len(batch) >= 40:
                flush()
            batch.append(t)
            size += len(t)
        flush()
    finally:
        save_json(cache_path(cfg), cache)


# ---------------------------------------------------------------- основное

def load_json(p, default):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def save_json(p, obj):
    p.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")


def load_config():
    cfg = dict(DEFAULTS)
    cfg.update(load_json(CONFIG, {}))
    return cfg


def run(cfg, log, dry=False, clean=False):
    mods = find_mods(cfg)
    if not mods:
        raise Stop("Моды не найдены. Проверьте пути к папкам.")
    skip_mods = [x.lower() for x in cfg.get("skip_mods", [])]
    mods = [m for m in mods if not any(x in m[0].lower() for x in skip_mods)]
    if cfg.get("only_enabled") and not clean:
        on = enabled_paths(cfg)
        if on is None:
            log("Файл dlc_load.json не найден: беру все моды.")
        else:
            mods = [m for m in mods if m[1].resolve() in on]
            if not mods:
                raise Stop("В плейсете нет включённых модов. Включите моды в лаунчере и запустите игру один раз.")
    if clean:
        n = 0
        for _, d in mods:
            for f in (d / "localization").glob(f"*/{OUT_PREFIX}*.yml"):
                f.unlink()
                n += 1
        log(f"Удалено файлов перевода: {n}")
        return

    if not SKIP.is_file():
        SKIP.write_text(DEFAULT_SKIP, encoding="utf-8")
    skip = [re.compile(l.strip()) for l in SKIP.read_text(encoding="utf-8").splitlines()
            if l.strip() and not l.startswith("#")]

    src, tgt = cfg["source_lang"], cfg["target_lang"]
    if src not in LANGS or tgt not in LANGS or src == tgt:
        raise Stop("Проверьте языки: исходный и язык перевода должны быть разными из списка " + ", ".join(LANGS))
    log(f"Перевод: {LANGS[src][2]} -> {LANGS[tgt][2]}")
    game = Path(cfg["game_dir"])
    log("Читаю локализацию игры...")
    ru_all = set(loc_keys(game, tgt))
    en_vanilla = set(loc_keys(game, src))
    if not ru_all:
        log("ВНИМАНИЕ: локализация игры на языке перевода не найдена, проверьте путь к игре.")
    log(f"Модов найдено: {len(mods)}")

    en_mod, any_mod = {}, {}
    for name, d in mods:
        ru_all.update(loc_keys(d, tgt))
        en_mod[d] = loc_keys(d, src)
    en_everything = en_vanilla.union(*[set(v) for v in en_mod.values()]) if en_mod else en_vanilla

    hand = {}
    if TRANSLATIONS.is_dir():
        for p in sorted(TRANSLATIONS.rglob("*.yml")):
            lang, keys = parse_yml(p)
            if lang == tgt:
                hand.update(keys)

    plan, texts = {}, []
    for name, d in mods:
        need = {k: v for k, v in en_mod[d].items() if k not in ru_all and k not in en_vanilla}
        created = {}
        if cfg.get("create_missing_keys") and src == "english":
            known = en_everything | ru_all
            created = {k: v for k, v in missing_objects(d, known).items()}
            need.update(created)
        need = {k: v for k, v in need.items() if not any(r.search(k) for r in skip)}
        if need:
            plan[d] = (name, need, len(created))
            texts += [v for k, v in need.items() if k not in hand]
    total = sum(len(p[1]) for p in plan.values())
    log(f"Модов с непереведённым текстом: {len(plan)}, строк: {total}")
    for d, (name, need, nc) in plan.items():
        log(f"  {name}: {len(need)} строк" + (f", из них создано ключей: {nc}" if nc else ""))
    if dry:
        return

    cache = load_json(cache_path(cfg), {})
    stopped = None
    try:
        translate_all(texts, cfg, cache, log)
    except Stop as e:
        stopped = str(e)
        log(stopped)

    written = left = 0
    for d, (name, need, nc) in plan.items():
        lines = []
        for k, v in need.items():
            tr = hand.get(k) or cache.get(v)
            if tr is None:
                if has_text(v):
                    left += 1
                tr = v                      # пока на исходном языке, чтобы не было имени ключа
            tr = tr.replace("\n", "\\n")
            lines.append(f' {k}: "{tr}"')
        out = d / "localization" / tgt
        out.mkdir(parents=True, exist_ok=True)
        (out / f"{OUT_PREFIX}{tgt}.yml").write_text(f"l_{tgt}:\n" + "\n".join(lines) + "\n", encoding="utf-8-sig", newline="\n")
        written += 1
    log(f"Готово. Файлов записано: {written}. Строк осталось без перевода: {left}.")
    if stopped:
        log("Перевод прерван. Запустите программу ещё раз позже: уже переведённое сохранено.")


# -------------------------------------------------------------------- окно

def gui():
    import tkinter as tk
    from tkinter import filedialog, scrolledtext, ttk
    cfg = load_config()
    root = tk.Tk()
    root.title("CK3 Переводчик модов")
    vars_ = {}
    rows = [("mods_dir", "Папка модов (Документы)"), ("workshop_dir", "Папка модов Steam Workshop"),
            ("game_dir", "Папка игры (game)")]
    for i, (key, label) in enumerate(rows):
        tk.Label(root, text=label, anchor="w").grid(row=i, column=0, sticky="w", padx=6, pady=3)
        v = tk.StringVar(value=cfg[key])
        vars_[key] = v
        tk.Entry(root, textvariable=v, width=80).grid(row=i, column=1, padx=4)
        tk.Button(root, text="...", command=lambda v=v: v.set(filedialog.askdirectory() or v.get())).grid(row=i, column=2, padx=4)
    tk.Label(root, text="Переводчик", anchor="w").grid(row=3, column=0, sticky="w", padx=6)
    eng = tk.StringVar(value=cfg["engine"])
    ttk.Combobox(root, textvariable=eng, values=list(ENGINES), state="readonly", width=20).grid(row=3, column=1, sticky="w", padx=4)
    lf = tk.Frame(root)
    lf.grid(row=4, column=1, sticky="e")
    src_v, tgt_v = tk.StringVar(value=cfg["source_lang"]), tk.StringVar(value=cfg["target_lang"])
    tk.Label(lf, text="С языка").pack(side="left")
    ttk.Combobox(lf, textvariable=src_v, values=list(LANGS), state="readonly", width=13).pack(side="left", padx=4)
    tk.Label(lf, text="на язык").pack(side="left")
    ttk.Combobox(lf, textvariable=tgt_v, values=list(LANGS), state="readonly", width=13).pack(side="left", padx=4)
    only = tk.BooleanVar(value=cfg["only_enabled"])
    tk.Checkbutton(root, text="Только моды из текущего плейсета", variable=only).grid(row=3, column=1, sticky="e")
    create = tk.BooleanVar(value=cfg["create_missing_keys"])
    tk.Checkbutton(root, text="Создавать отсутствующие ключи (решения, взаимодействия, черты)", variable=create).grid(row=4, column=1, sticky="w")
    again = tk.BooleanVar(value=False)
    tk.Checkbutton(root, text="Перевести заново выбранным переводчиком (не брать из кэша)", variable=again).grid(row=5, column=1, sticky="w")
    keys = tk.LabelFrame(root, text="Ключи API и модели (нужны только для выбранного переводчика)")
    keys.grid(row=6, column=0, columnspan=3, sticky="we", padx=6, pady=4)
    fields = [("openrouter_key", "OpenRouter: ключ", True), ("openrouter_model", "OpenRouter: модель", False),
              ("silk_key", "SilkCompute: ключ", True), ("silk_model", "SilkCompute: модель", False),
              ("deepl_key", "DeepL: ключ", True), ("google_cloud_key", "Google Cloud: ключ", True),
              ("style", "Пожелания к стилю (для нейросети)", False)]
    for i, (key, label, secret) in enumerate(fields):
        tk.Label(keys, text=label, anchor="w").grid(row=i // 2, column=(i % 2) * 2, sticky="w", padx=4, pady=2)
        v = tk.StringVar(value=cfg.get(key, ""))
        vars_[key] = v
        tk.Entry(keys, textvariable=v, width=42, show="*" if secret else "").grid(row=i // 2, column=(i % 2) * 2 + 1, padx=4)
    box = scrolledtext.ScrolledText(root, width=110, height=22)
    box.grid(row=8, column=0, columnspan=3, padx=6, pady=6)

    def log(s):
        root.after(0, lambda: (box.insert("end", s + "\n"), box.see("end")))

    def start(dry=False, clean=False):
        for k, v in vars_.items():
            cfg[k] = v.get()
        cfg["engine"], cfg["create_missing_keys"] = eng.get(), create.get()
        cfg["only_enabled"] = only.get()
        cfg["source_lang"], cfg["target_lang"] = src_v.get(), tgt_v.get()
        cfg["retranslate"] = False
        save_json(CONFIG, cfg)
        cfg["retranslate"] = again.get()

        def work():
            try:
                run(cfg, log, dry=dry, clean=clean)
            except Stop as e:
                log(str(e))
            except Exception as e:  # noqa
                log(f"Ошибка: {e!r}")
        threading.Thread(target=work, daemon=True).start()

    bar = tk.Frame(root)
    bar.grid(row=7, column=0, columnspan=3, pady=4)
    tk.Button(bar, text="Перевести", width=18, command=start).pack(side="left", padx=4)
    tk.Button(bar, text="Только отчёт", width=18, command=lambda: start(dry=True)).pack(side="left", padx=4)
    tk.Button(bar, text="Удалить перевод", width=18, command=lambda: start(clean=True)).pack(side="left", padx=4)
    root.mainloop()


def main():
    args = sys.argv[1:]
    if not args:
        return gui()
    if sys.stdout:
        sys.stdout.reconfigure(encoding="utf-8")
    cfg = load_config()
    if not CONFIG.is_file():
        save_json(CONFIG, cfg)
    try:
        run(cfg, print if sys.stdout else (lambda s: None), dry="--dry" in args, clean="--clean" in args)
    except Stop as e:
        print(e)


if __name__ == "__main__":
    main()
