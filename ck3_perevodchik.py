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
    "same_is_untranslated": False, # считать непереведённым текст, который совпадает с исходным
    "threads": 4,                  # сколько запросов к переводчику идёт одновременно
    "only_enabled": True,          # только моды из текущего плейсета (dlc_load.json)
    "skip_mods": [],               # моды, которые не трогать (часть названия)
}
DEFAULT_SKIP = "# Регулярные выражения: ключи, которые не надо переводить (по одному в строке)\n# Пример: ^my_mod_secret_event\n"

LINE = re.compile(r'^\s*([^\s:#"]+):\d*\s*(".*")')
HEADER = re.compile(r'^\ufeff?\s*l_(\w+)\s*:')


class Stop(Exception):
    pass


CANCEL = threading.Event()         # выставляется кнопкой «Отмена»
CANCELLED = "Перевод отменён."
RETRY_CODES = {408, 429, 500, 502, 503, 504, 529}


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

REQUEST_LIMIT = 180                # секунд на один запрос, дольше не ждём


def _fetch(req, box):
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            box["body"] = r.read().decode("utf-8", "replace")
    except BaseException as e:      # noqa: ошибку разбирает вызывающий поток
        box["err"] = e


def http(url, data=None, headers=None):
    """Запрос к сервису. Временные отказы (429, 5xx, обрыв связи, зависание) повторяются с паузой.

    Сам запрос идёт в отдельном потоке: сервис может держать соединение открытым сколь угодно долго,
    а нам нужно и общее ограничение по времени, и мгновенная реакция на «Отмену».
    """
    last = ""
    for pause in (0, 3, 8, 20):
        if pause and CANCEL.wait(pause):
            raise Stop(CANCELLED)
        if CANCEL.is_set():
            raise Stop(CANCELLED)
        box = {}
        t = threading.Thread(target=_fetch, args=(urllib.request.Request(url, data=data, headers=headers or {}), box),
                             daemon=True)
        t.start()
        deadline = time.time() + REQUEST_LIMIT
        while t.is_alive() and time.time() < deadline:
            t.join(0.2)
            if CANCEL.is_set():
                raise Stop(CANCELLED)   # зависший запрос бросаем, его поток завершится сам
        if t.is_alive():
            last = f"Сервис перевода не ответил за {REQUEST_LIMIT} секунд. Перевод остановлен, сделанное сохранено."
            continue
        err = box.get("err")
        if err is None:
            return box["body"]
        if isinstance(err, urllib.error.HTTPError):
            hint = {401: " Проверьте ключ API.", 402: " На счёте сервиса закончились средства.",
                    404: " Проверьте название модели.", 429: " Слишком много запросов, попробуйте позже."}.get(err.code, "")
            detail = err.read().decode("utf-8", "replace")[:200]
            last = f"Сервис перевода ответил ошибкой {err.code}.{hint} {detail} Перевод остановлен, сделанное сохранено."
            if err.code not in RETRY_CODES:
                break
        elif isinstance(err, OSError):
            last = f"Нет связи с сервисом перевода: {err}"
        else:
            last = f"Ошибка запроса: {err!r}"
            break
    raise Stop(last)


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


def fetch_models(base, key=""):
    """Список моделей сервиса с API в формате OpenAI (GET /models)."""
    headers = {"Authorization": "Bearer " + key} if key else {}
    body = json.loads(http(base.rstrip("/") + "/models", None, headers))
    data = body.get("data", body) if isinstance(body, dict) else body
    return sorted({m["id"] for m in data if isinstance(m, dict) and m.get("id")})


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


def translate_batch(batch, cfg):
    """Перевод одной пачки строк -> {исходная строка: перевод}. Ничего общего не трогает."""
    engine = ENGINES[cfg["engine"]]

    def call(lines):
        res = engine(lines, cfg)
        if cfg["engine"] not in LLM_ENGINES:
            time.sleep(0.4)
        return res

    prot = [protect(t) for t in batch]
    res = call([p[0] for p in prot])
    if len(res) != len(batch):          # сервис склеил строки: по одной
        res = [(call([p[0]]) or [""])[0] for p in prot]
    out = {}
    for src, (ptext, tokens), tr in zip(batch, prot, res):
        text = restore(tr, tokens)
        if text is None:                # плейсхолдеры повреждены: переводим куски между ними
            parts = re.split(r'(\{\d+\})', ptext)
            idx = [i for i, p in enumerate(parts) if not re.fullmatch(r'\{\d+\}', p) and re.search(r'[^\W\d_]', p)]
            if idx:
                got = call([parts[i].strip() for i in idx])
                if len(got) == len(idx):
                    for i, g in zip(idx, got):
                        lead = parts[i][:len(parts[i]) - len(parts[i].lstrip())]
                        tail = parts[i][len(parts[i].rstrip()):]
                        parts[i] = lead + g + tail
            text = restore("".join(parts), tokens) or src
        out[src] = text
    return out


def translate_all(texts, cfg, cache, log):
    """Дополняет cache переводами для texts. Пачки строк переводятся в несколько потоков."""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    again = cfg.get("retranslate")
    todo = [t for t in dict.fromkeys(texts) if (again or t not in cache) and has_text(t)]
    batches, batch, size = [], [], 0
    for t in todo:
        if batch and (size + len(t) > 3500 or len(batch) >= 40):
            batches.append(batch)
            batch, size = [], 0
        batch.append(t)
        size += len(t)
    if batch:
        batches.append(batch)
    try:
        threads = max(1, min(16, int(cfg.get("threads") or 1)))
    except (TypeError, ValueError):
        threads = 1
    if cfg["engine"] == "google_free":
        threads = 1                     # бесплатный Google за параллельные запросы блокирует
    threads = min(threads, max(1, len(batches)))
    log(f"К переводу: {len(todo)} строк (в кэше уже {len(cache)}), потоков: {threads}")

    lock = threading.Lock()             # кэш и счётчик меняет только один поток за раз
    state = {"done": 0, "saved": time.time()}
    abort = threading.Event()           # один поток упал: остальные не начинают новые пачки

    def work(batch):
        if CANCEL.is_set():
            raise Stop(CANCELLED)
        if abort.is_set():
            return
        result = translate_batch(batch, cfg)
        with lock:
            cache.update(result)
            state["done"] += len(batch)
            if time.time() - state["saved"] > 5:
                save_json(cache_path(cfg), cache)
                state["saved"] = time.time()
            log(f"  переведено {state['done']} из {len(todo)}")

    error = None
    try:
        if threads == 1:
            for b in batches:
                work(b)
        else:
            with ThreadPoolExecutor(max_workers=threads) as pool:
                futures = [pool.submit(work, b) for b in batches]
                for f in as_completed(futures):
                    try:
                        f.result()
                    except Stop as e:
                        if error is None:
                            error = e
                            abort.set()
                            for other in futures:
                                other.cancel()
                    except Exception:
                        pass            # уже отменённая пачка
    finally:
        with lock:
            save_json(cache_path(cfg), cache)
    if error is not None:
        raise error


# ---------------------------------------------------------------- основное

def load_json(p, default):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


SAVE_LOCK = threading.Lock()


def save_json(p, obj):
    """Запись через временный файл: если программу оборвут на середине, прежний файл останется целым."""
    import os
    text = json.dumps(obj, ensure_ascii=False, indent=1)
    with SAVE_LOCK:
        tmp = p.with_name(p.name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, p)


def load_cache(cfg, log):
    """Кэш переводов. Если основной файл пуст или испорчен, берётся запасная копия."""
    p = cache_path(cfg)
    bak = p.with_name(p.name + ".bak")
    cache = load_json(p, None)
    if not isinstance(cache, dict) or (not cache and bak.is_file()):
        old = load_json(bak, None)
        if isinstance(old, dict) and old:
            log(f"Файл кэша пуст или испорчен, взята запасная копия: {len(old)} строк.")
            cache = old
    if not isinstance(cache, dict):
        cache = {}
    if cache:
        save_json(bak, cache)           # запасная копия на начало этого запуска
    return cache


def seed_cache_from_mods(plan, cache, cfg):
    """Переводы, уже записанные этой программой в моды, возвращаются в кэш (если кэш потерян)."""
    if cfg.get("retranslate"):
        return 0
    added = 0
    for d, item in plan.items():
        need = item[1]
        done = {}
        for f in own_files(d):
            lang, keys = parse_yml(f)
            if lang == cfg["target_lang"]:
                done.update(keys)
        for k, src in need.items():
            tr = done.get(k)
            if tr and tr != src and src not in cache and has_text(src):
                cache[src] = tr
                added += 1
    return added


def load_config():
    cfg = dict(DEFAULTS)
    cfg.update(load_json(CONFIG, {}))
    return cfg


def own_files(mod_dir):
    """Файлы перевода, которые создала эта программа (в любой подпапке localization)."""
    d = mod_dir / "localization"
    return [f for f in d.rglob(f"{OUT_PREFIX}*.yml")] if d.is_dir() else []


def run(cfg, log, dry=False, clean=False):
    CANCEL.clear()
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
            for f in own_files(d):
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
    ru_text = loc_keys(game, tgt)      # ключ -> текст на языке перевода (игра и все моды)
    ru_all = set(ru_text)
    en_vanilla = set(loc_keys(game, src))
    if not ru_all:
        log("ВНИМАНИЕ: локализация игры на языке перевода не найдена, проверьте путь к игре.")
    log(f"Модов найдено: {len(mods)}")

    en_mod, any_mod = {}, {}
    for name, d in mods:
        ru_text.update(loc_keys(d, tgt))
        en_mod[d] = loc_keys(d, src)
    ru_all = set(ru_text)
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
        same = 0
        if cfg.get("same_is_untranslated"):
            # «перевод» есть, но он слово в слово повторяет исходный текст
            for k, v in en_mod[d].items():
                if k not in need and k not in en_vanilla and ru_text.get(k) == v and has_text(v):
                    need[k] = v
                    same += 1
        created = {}
        if cfg.get("create_missing_keys") and src == "english":
            known = en_everything | ru_all
            created = {k: v for k, v in missing_objects(d, known).items()}
            need.update(created)
        need = {k: v for k, v in need.items() if not any(r.search(k) for r in skip)}
        if need:
            plan[d] = (name, need, len(created), same)
            texts += [v for k, v in need.items() if k not in hand]
    total = sum(len(p[1]) for p in plan.values())
    log(f"Модов с непереведённым текстом: {len(plan)}, строк: {total}")
    for d, (name, need, nc, same) in plan.items():
        log(f"  {name}: {len(need)} строк" + (f", из них создано ключей: {nc}" if nc else "")
            + (f", совпадает с исходным: {same}" if same else ""))
    if dry:
        return

    cache = load_cache(cfg, log)
    seeded = seed_cache_from_mods(plan, cache, cfg)
    if seeded:
        log(f"Из файлов перевода в модах возвращено в кэш: {seeded} строк.")
        save_json(cache_path(cfg), cache)
    stopped = None
    try:
        translate_all(texts, cfg, cache, log)
    except Stop as e:
        stopped = str(e)
        log(stopped)

    written = left = 0
    for d, (name, need, nc, same) in plan.items():
        lines = []
        for k, v in need.items():
            tr = hand.get(k) or cache.get(v)
            if tr is None:
                if has_text(v):
                    left += 1
                tr = v                      # пока на исходном языке, чтобы не было имени ключа
            tr = tr.replace("\n", "\\n")
            lines.append(f' {k}: "{tr}"')
        # папка replace: наши строки заменяют одноимённые строки самого мода
        out = d / "localization" / tgt / "replace"
        out.mkdir(parents=True, exist_ok=True)
        target = out / f"{OUT_PREFIX}{tgt}.yml"
        for old in own_files(d):
            if old != target and old.name == target.name:
                old.unlink()                # файл прежних версий программы лежал уровнем выше
        target.write_text(f"l_{tgt}:\n" + "\n".join(lines) + "\n", encoding="utf-8-sig", newline="\n")
        written += 1
    log(f"Готово. Файлов записано: {written}. Строк осталось без перевода: {left}.")
    if stopped == CANCELLED:
        log("Уже переведённое сохранено и записано в моды. Чтобы продолжить, нажмите «Перевести» ещё раз.")
    elif stopped:
        log("Перевод прерван. Запустите программу ещё раз позже: уже переведённое сохранено.")


# -------------------------------------------------------------------- окно

def gui():
    import tkinter as tk
    from tkinter import filedialog, scrolledtext, ttk
    cfg = load_config()
    root = tk.Tk()
    root.title("CK3 Переводчик модов")

    # фоновые потоки не трогают окно сами: они кладут действие в очередь, главный поток его выполняет
    import queue
    ui_queue = queue.Queue()
    ui = ui_queue.put

    def poll():
        try:
            while True:
                ui_queue.get_nowait()()
        except queue.Empty:
            pass
        root.after(50, poll)
    root.after(50, poll)

    # Ctrl+C/V/X/A при русской (и любой не латинской) раскладке: Tk сам их не узнаёт
    def hotkey(e):
        if e.keysym.lower() in ("c", "v", "x", "a"):
            return None                      # латинская раскладка: Tk справится сам
        name = {67: "<<Copy>>", 86: "<<Paste>>", 88: "<<Cut>>", 65: "<<SelectAll>>"}.get(e.keycode)
        if name:
            e.widget.event_generate(name)
            return "break"
    root.bind_all("<Control-KeyPress>", hotkey)

    # правая кнопка мыши: меню «Вырезать / Копировать / Вставить»
    menu = tk.Menu(root, tearoff=0)
    for label, name in (("Вырезать", "<<Cut>>"), ("Копировать", "<<Copy>>"), ("Вставить", "<<Paste>>")):
        menu.add_command(label=label, command=lambda n=name: root.focus_get() and root.focus_get().event_generate(n))

    def popup(e):
        e.widget.focus_set()
        menu.tk_popup(e.x_root, e.y_root)
    root.bind_class("Entry", "<Button-3>", popup)
    root.bind_class("Text", "<Button-3>", popup)
    vars_ = {}
    rows = [("mods_dir", "Папка модов (Документы)"), ("workshop_dir", "Папка модов Steam Workshop"),
            ("game_dir", "Папка игры (game)")]
    for i, (key, label) in enumerate(rows):
        tk.Label(root, text=label, anchor="w").grid(row=i, column=0, sticky="w", padx=6, pady=3)
        v = tk.StringVar(value=cfg[key])
        vars_[key] = v
        tk.Entry(root, textvariable=v, width=60).grid(row=i, column=1, padx=4, sticky="we")
        tk.Button(root, text="...", command=lambda v=v: v.set(filedialog.askdirectory() or v.get())).grid(row=i, column=2, padx=4)
    root.columnconfigure(1, weight=1)
    root.rowconfigure(8, weight=1)
    root.minsize(760, 560)

    tk.Label(root, text="Переводчик", anchor="w").grid(row=3, column=0, sticky="w", padx=6)
    line = tk.Frame(root)
    line.grid(row=3, column=1, columnspan=2, sticky="w", pady=2)
    eng = tk.StringVar(value=cfg["engine"])
    ttk.Combobox(line, textvariable=eng, values=list(ENGINES), state="readonly", width=16).pack(side="left", padx=4)
    src_v, tgt_v = tk.StringVar(value=cfg["source_lang"]), tk.StringVar(value=cfg["target_lang"])
    tk.Label(line, text="С языка").pack(side="left", padx=(16, 0))
    ttk.Combobox(line, textvariable=src_v, values=list(LANGS), state="readonly", width=13).pack(side="left", padx=4)
    tk.Label(line, text="на язык").pack(side="left")
    ttk.Combobox(line, textvariable=tgt_v, values=list(LANGS), state="readonly", width=13).pack(side="left", padx=4)
    threads_v = tk.StringVar(value=str(cfg.get("threads", 4)))
    tk.Label(line, text="Потоков").pack(side="left", padx=(16, 0))
    tk.Spinbox(line, from_=1, to=16, width=3, textvariable=threads_v).pack(side="left", padx=4)

    checks = tk.Frame(root)
    checks.grid(row=4, column=1, columnspan=2, sticky="w")
    only = tk.BooleanVar(value=cfg["only_enabled"])
    tk.Checkbutton(checks, text="Только моды из текущего плейсета", variable=only).pack(anchor="w")
    create = tk.BooleanVar(value=cfg["create_missing_keys"])
    tk.Checkbutton(checks, text="Создавать отсутствующие ключи (решения, взаимодействия, черты)", variable=create).pack(anchor="w")
    same_v = tk.BooleanVar(value=cfg.get("same_is_untranslated", False))
    tk.Checkbutton(checks, text="Считать непереведённым текст, который совпадает с исходным", variable=same_v).pack(anchor="w")
    again = tk.BooleanVar(value=False)
    tk.Checkbutton(checks, text="Перевести заново выбранным переводчиком (не брать из кэша)", variable=again).pack(anchor="w")
    keys = tk.LabelFrame(root, text="Ключи API и модели (нужны только для выбранного переводчика)")
    keys.grid(row=6, column=0, columnspan=3, sticky="we", padx=6, pady=4)
    keys.columnconfigure(1, weight=1)
    keys.columnconfigure(3, weight=1)
    fields = [("openrouter_key", "OpenRouter: ключ", True), ("openrouter_model", "OpenRouter: модель", False),
              ("silk_key", "SilkCompute: ключ", True), ("silk_model", "SilkCompute: модель", False),
              ("deepl_key", "DeepL: ключ", True), ("google_cloud_key", "Google Cloud: ключ", True),
              ("style", "Пожелания к стилю (для нейросети)", False)]
    for i, (key, label, secret) in enumerate(fields):
        tk.Label(keys, text=label, anchor="w").grid(row=i // 2, column=(i % 2) * 2, sticky="w", padx=4, pady=2)
        v = tk.StringVar(value=cfg.get(key, ""))
        vars_[key] = v
        if not key.endswith("_model"):
            tk.Entry(keys, textvariable=v, width=24, show="*" if secret else "").grid(
                row=i // 2, column=(i % 2) * 2 + 1, padx=4, sticky="we", columnspan=3 if key == "style" else 1)
            continue

        # поле модели: выпадающий список с подсказкой по набранной части и кнопка проверки
        service = key[:-len("_model")]              # openrouter | silk
        combo = ttk.Combobox(keys, textvariable=v, width=24)
        combo.grid(row=i // 2, column=(i % 2) * 2 + 1, padx=4, sticky="we")
        models = []

        def base_key(service=service):
            if service == "openrouter":
                return "https://openrouter.ai/api/v1", vars_["openrouter_key"].get().strip()
            return cfg["silk_base"], vars_["silk_key"].get().strip()

        def load(combo=combo, models=models, base_key=base_key, service=service, quiet=True):
            base, k = base_key()                    # поля окна читаем только в главном потоке

            def work():
                try:
                    got = fetch_models(base, k)
                except Stop as e:
                    if not quiet:
                        log(f"{service}: список моделей не получен. {e}")
                    return
                models[:] = got
                ui(lambda: combo.configure(values=got))
                if not quiet:
                    log(f"{service}: моделей в списке {len(got)}")
            threading.Thread(target=work, daemon=True).start()

        def suggest(e, combo=combo, models=models, v=v):
            if e.keysym in ("Up", "Down", "Left", "Right", "Return", "Escape", "Tab", "Home", "End") or not models:
                return
            typed = combo.get()[:combo.index("insert")] if combo.selection_present() else combo.get()
            low = typed.lower()
            combo.configure(values=[m for m in models if low in m.lower()] if low else models)
            if e.keysym in ("BackSpace", "Delete") or not low or len(e.char) != 1:
                return
            # дописать название: сначала совпадение с начала, потом после «/» (имя без поставщика)
            match = next((m for m in models if m.lower().startswith(low)), None)
            if match is None and "/" not in typed:
                match = next((m for m in models if m.lower().split("/")[-1].startswith(low)), None)
                if match:
                    v.set(match)
                    start_at = len(match) - len(match.split("/")[-1]) + len(typed)
                    combo.icursor(start_at)
                    combo.selection_range(start_at, "end")
                return
            if match:
                v.set(match)
                combo.icursor(len(typed))
                combo.selection_range(len(typed), "end")

        combo.bind("<KeyRelease>", suggest)
        combo.configure(postcommand=lambda load=load, models=models: (None if models else load(quiet=False)))
        status = tk.Label(keys, text="", width=3)

        def test(base_key=base_key, v=v, status=status, service=service):
            status.configure(text="...", fg="black")
            model = v.get().strip()
            base, k = base_key()

            def work():
                try:
                    t0 = time.time()
                    res = llm(base, k, model, ["Hello"], cfg)
                    ok = bool(res)
                    msg = (f"{service}: модель {model} доступна, ответ за {time.time() - t0:.1f} с: {res[0][:60]}" if ok
                           else f"{service}: модель {model} ответила, но не в нужном формате. Для перевода она не подходит.")
                except Stop as e:
                    ok, msg = False, f"{service}: модель {model} недоступна. {e}"
                log(msg)
                ui(lambda: status.configure(text="✓" if ok else "✗", fg="green" if ok else "red"))
            threading.Thread(target=work, daemon=True).start()

        cell = tk.Frame(keys)
        cell.grid(row=i // 2, column=4, padx=2)
        tk.Button(cell, text="Тест", command=test).pack(side="left")
        status.pack(in_=cell, side="left")
        if service == "openrouter" or vars_.get(service + "_key") and vars_[service + "_key"].get():
            load()
    box = scrolledtext.ScrolledText(root, width=90, height=14)
    box.grid(row=8, column=0, columnspan=3, padx=6, pady=6, sticky="nsew")

    def log(s):
        ui(lambda: (box.insert("end", s + "\n"), box.see("end")))

    running = {"on": False}

    def start(dry=False, clean=False):
        if running["on"]:
            log("Работа уже идёт. Дождитесь конца или нажмите «Отмена».")
            return
        for k, v in vars_.items():
            cfg[k] = v.get()
        cfg["engine"], cfg["create_missing_keys"] = eng.get(), create.get()
        cfg["only_enabled"] = only.get()
        cfg["source_lang"], cfg["target_lang"] = src_v.get(), tgt_v.get()
        cfg["same_is_untranslated"] = same_v.get()
        try:
            cfg["threads"] = max(1, min(16, int(threads_v.get())))
        except ValueError:
            cfg["threads"] = 1
        cfg["retranslate"] = False
        save_json(CONFIG, cfg)
        cfg["retranslate"] = again.get()

        running["on"] = True

        def work():
            try:
                run(cfg, log, dry=dry, clean=clean)
            except Stop as e:
                log(str(e))
            except Exception as e:  # noqa
                log(f"Ошибка: {e!r}")
            finally:
                running["on"] = False
        threading.Thread(target=work, daemon=True).start()

    def cancel():
        if running["on"]:
            CANCEL.set()
            log("Отмена: останавливаю запросы, сохраняю уже переведённое...")
        else:
            log("Сейчас ничего не выполняется.")

    bar = tk.Frame(root)
    bar.grid(row=7, column=0, columnspan=3, pady=4)
    tk.Button(bar, text="Перевести", width=18, command=start).pack(side="left", padx=4)
    tk.Button(bar, text="Только отчёт", width=18, command=lambda: start(dry=True)).pack(side="left", padx=4)
    tk.Button(bar, text="Удалить перевод", width=18, command=lambda: start(clean=True)).pack(side="left", padx=4)
    tk.Button(bar, text="Отмена", width=12, command=cancel).pack(side="left", padx=4)
    root.mainloop()
    CANCEL.set()                        # окно закрыто: останавливаем запросы
    limit = time.time() + 5
    while running["on"] and time.time() < limit:
        time.sleep(0.1)                 # даём записать кэш и файлы в моды
    import os
    with SAVE_LOCK:                     # не обрываем запись на середине
        os._exit(0)


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
