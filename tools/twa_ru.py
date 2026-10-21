#!/usr/bin/env python3
"""twa_ru - установщик русской локализации для клиента Total War: Arena (Revival).

Ничего не скачивает и не содержит файлов игры. Берёт ваши собственные
data/local_en.pack и data/text.pack, подставляет русский текст из папки
translations/ru и записывает паки обратно. Оригиналы сохраняются в
<игра>/data/backup_ru и всегда используются как источник при повторном запуске.

Важное ограничение клиента: размер КАЖДОГО файла внутри пака должен остаться
байт-в-байт прежним, иначе игра падает на первой картинке. Поэтому русская
строка, которая «не влезает» в бюджет файла, пропускается (остаётся английской),
а запас добивается пробелами. Список пропущенных строк даёт команда `report`.

Команды:
    apply    применить перевод
    restore  вернуть оригиналы
    report   показать, что не влезло (и сохранить CSV)
    sheet    выгрузить таблицы для переводчиков (английский текст берётся
             из ваших паков и в репозиторий не попадает)

Только стандартная библиотека Python 3.8+.
Формат пака и ключи шифрования индекса описаны в проекте RPFM (MIT).
"""
import argparse, csv, hashlib, json, re, shutil, struct, sys
from pathlib import Path

# --- ключи индекса пака (RPFM, rpfm_lib/src/encryption) -----------------------
STRING_KEY = b"#:AhppdV-!PEfz&}[]Nv?6w4guU%dF5.fq:n*-qGuhBJJBm&?2tPy!geW/+k#pG?"
U32_KEY = 0xE10B73F4
HEADER_LEN = 48          # 24 байта заголовка + 20 байт расширенного заголовка + 4
END_FIELD = 40           # смещение поля «конец данных» (за ним идёт подпись)
TRAILER = 256            # подпись в конце пака, копируется как есть

EXPECTED = {"local_en.pack": 116373279, "text.pack": 7663873}
BACKUP_DIR = "backup_ru"
BS = chr(92)
CYR = re.compile("[а-яА-ЯёЁ]")
# разметка, которая обязана совпадать в оригинале и переводе
TOKENS = re.compile("|".join([
    r"(?<![0-9])%[-+ #0]*[0-9]*(?:[.][0-9]+)?(?:[sdiuxXfFeEgGcpSn]|%)", r"[{][^}]*[}]", r"[[][[].*?[]][]]",
    r"<[^>]*>", "\r", "\n", "\t"]))


# --- формат пака ---------------------------------------------------------------
class Pack:
    def __init__(self, path):
        self.raw = Path(path).read_bytes()
        b = self.raw
        magic, _bm, _pc, _ps, count, idx_size = struct.unpack("<4sIIIII", b[:24])
        if magic != b"PFH5":
            raise SystemExit(f"{path}: это не пак PFH5")
        self.header = b[:HEADER_LEN]
        pos, entries = HEADER_LEN, []
        for i in range(count):
            key = count - 1 - i
            size = struct.unpack("<I", b[pos:pos + 4])[0] ^ U32_KEY ^ (~key & 0xFFFFFFFF)
            pos += 4
            name = bytearray()
            j = 0
            while True:
                c = b[pos] ^ STRING_KEY[j % 64] ^ (~size & 0xFF)
                pos += 1
                j += 1
                if c == 0:
                    break
                name.append(c)
            entries.append((name.decode("latin1"), size))
        if pos != HEADER_LEN + idx_size:
            raise SystemExit(f"{path}: повреждён индекс пака")
        self.files = []
        for name, size in entries:
            self.files.append([name, b[pos:pos + size]])
            pos += size
        if len(b) - pos != TRAILER:
            raise SystemExit(f"{path}: неожиданный хвост пака")
        self.trailer = b[pos:]

    def build(self):
        n = len(self.files)
        idx = bytearray()
        for i, (name, data) in enumerate(self.files):
            key, size = n - 1 - i, len(data)
            idx += struct.pack("<I", size ^ U32_KEY ^ (~key & 0xFFFFFFFF))
            idx += bytes(c ^ STRING_KEY[j % 64] ^ (~size & 0xFF)
                         for j, c in enumerate(name.encode("latin1") + b"\0"))
        head = bytearray(self.header)
        struct.pack_into("<II", head, 16, n, len(idx))
        body = bytes(head) + bytes(idx) + b"".join(d for _, d in self.files)
        struct.pack_into("<I", head, END_FIELD, len(body))
        return bytes(head) + bytes(idx) + b"".join(d for _, d in self.files) + self.trailer


# --- .loc (бинарный) и .txt (UTF-16, «Old style») -----------------------------
def loc_read(d):
    count = struct.unpack("<I", d[10:14])[0]
    o, out = 14, []
    for _ in range(count):
        n = struct.unpack("<H", d[o:o + 2])[0]; o += 2
        key = d[o:o + 2 * n].decode("utf-16le"); o += 2 * n
        n = struct.unpack("<H", d[o:o + 2])[0]; o += 2
        val = d[o:o + 2 * n].decode("utf-16le"); o += 2 * n
        out.append([key, val, d[o]]); o += 1
    return d[:14], out


def loc_write(head, rows):
    r = bytearray(head)
    for k, v, f in rows:
        r += struct.pack("<H", len(k)) + k.encode("utf-16le")
        r += struct.pack("<H", len(v)) + v.encode("utf-16le") + bytes([f])
    return bytes(r)


TXT_LINE = re.compile(r"^\{([^}]+)\}\t")


# --- переводы -----------------------------------------------------------------
def unescape(s):
    out, i = [], 0
    while i < len(s):
        c = s[i]
        if c == BS and i + 1 < len(s):
            n = s[i + 1]
            out.append({"n": "\n", "t": "\t", "r": "\r", BS: BS}.get(n, c + n))
            i += 2
        else:
            out.append(c); i += 1
    return "".join(out)


def escape(s):
    return s.replace(BS, BS * 2).replace("\t", BS + "t").replace("\r", BS + "r").replace("\n", BS + "n")


def load_translations(tdir):
    """-> {имя_таблицы: {ключ: русский текст}}"""
    tables = {}
    for f in sorted(Path(tdir).glob("*.tsv")):
        t = {}
        for ln, line in enumerate(f.read_text(encoding="utf-8").split("\n"), 1):
            if not line.strip() or line.startswith("#"):
                continue
            if "\t" not in line:
                print(f"! {f.name}:{ln}: нет табуляции, строка пропущена"); continue
            k, v = line.split("\t", 1)
            t[k] = unescape(v)
        tables[f.stem] = t
    return tables


def table_name(path):
    return path.split(BS)[-1].rsplit(".", 1)[0]


ABBR = {1: [], 2: [], 3: [], 4: []}     # этап -> [(что, на что)], см. translations/abbreviations.tsv


def load_abbr(path):
    ABBR[1], ABBR[2], ABBR[3], ABBR[4] = [], [], [], []
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        parts = line.split(chr(9))
        if len(parts) == 3 and parts[0] in ("1", "2", "3", "4"):
            ABBR[int(parts[0])].append((parts[1], parts[2]))


def abbreviate(text, stage):
    """Сокращает текст правилами этапа 1..stage (разметка в [[...]] и {...} не трогается)."""
    parts = re.split(r"(\[\[.*?\]\]|\{[^}]*\}|(?<![0-9])%[-+ #0]*[0-9]*(?:[.][0-9]+)?(?:[sdiuxXfFeEgGcpSn]|%))", text)
    for st in range(1, stage + 1):
        for a, b in ABBR[st]:
            for i in range(0, len(parts), 2):
                parts[i] = parts[i].replace(a, b)
                if a[:1].islower():
                    parts[i] = parts[i].replace(a[:1].upper() + a[1:], b[:1].upper() + b[1:])
    out = "".join(parts).strip() if stage >= 4 else "".join(parts)
    if text[:1].isupper() and out[:1].islower():
        out = out[:1].upper() + out[1:]
    return out


def fit_once(cands):
    cands = sorted(cands, key=lambda c: -c[2])
    net = sum(c[2] for c in cands)
    skipped = []
    while net > 0 and cands:
        c = cands.pop(0); net -= c[2]; skipped.append(c)
    return cands, -net, skipped


def fit(cands):
    """cands: [[индекс, новый_текст, рост_в_символах]] -> (что оставить, запас символов, пропущенное).
    Пока суммарный рост > 0, выкидываем строки с наибольшим ростом; для выкинутых
    пробуем сокращения (этап 1: синонимы, 2: сокращения слов, 3: жёсткие сокращения названий, 4: удаление слов-оценок) и повторяем."""
    kept, slack, skipped = fit_once(cands)
    for stage in (1, 2, 3, 4):
        if not skipped or not ABBR[stage]:
            continue
        retry = []
        for i, new, g in skipped:
            short = abbreviate(new, stage)
            retry.append([i, short, g - (len(new) - len(short))])
        kept, slack, skipped = fit_once(kept + retry)
    return kept, slack, skipped


def patch_loc(data, tr, skipped_log, name):
    head, rows = loc_read(data)
    cands, bad = [], 0
    for i, (k, v, _f) in enumerate(rows):
        if k not in tr:
            continue
        new = tr[k]
        if tail := v[len(v.rstrip()):]:
            if not new.endswith(tail):
                new = new.rstrip(" ") + tail
        if sorted(TOKENS.findall(v)) != sorted(TOKENS.findall(new)):
            skipped_log.append((name, k, "разметка", len(v), len(new))); continue
        cands.append([i, new, len(new) - len(v)])
    keep, slack, skipped = fit(cands)
    for i, new, _g in skipped:
        skipped_log.append((name, rows[i][0], "не влезло", len(rows[i][1]), len(new)))
    if not keep:
        return data, 0
    for i, new, _g in keep:
        rows[i][1] = new
    rows[keep[0][0]][1] += " " * slack
    out = loc_write(head, rows)
    assert len(out) == len(data), name
    return out, len(keep)


def patch_txt(data, tr, skipped_log, name):
    text = data.decode("utf-16")
    lines = text.split("\r\n")
    cands = []
    for i, line in enumerate(lines):
        m = TXT_LINE.match(line)
        if not m or m.group(1) not in tr:
            continue
        old, new = line[m.end():], tr[m.group(1)]
        if re.search("[\r\n\t]", new):
            continue
        if sorted(TOKENS.findall(old)) != sorted(TOKENS.findall(new)):
            skipped_log.append((name, m.group(1), "разметка", len(old), len(new))); continue
        cands.append([i, line[:m.end()] + new, len(new) - len(old)])
    keep, slack, skipped = fit(cands)
    for i, newline, _g in skipped:
        skipped_log.append((name, TXT_LINE.match(lines[i]).group(1), "не влезло", 0, len(newline)))
    if not keep:
        return data, 0
    for i, newline, _g in keep:
        lines[i] = newline
    lines[keep[0][0]] += " " * slack
    out = b"\xff\xfe" + "\r\n".join(lines).encode("utf-16le")
    assert len(out) == len(data), name
    return out, len(keep)


def patch_pack(pack, tables, log):
    stats = {}
    for item in pack.files:
        path, data = item
        t = tables.get(table_name(path))
        if not t:
            continue
        if path.endswith(".loc"):
            item[1], n = patch_loc(data, t, log, path)
        elif path.endswith(".txt"):
            item[1], n = patch_txt(data, t, log, path)
        else:
            continue
        if n:
            stats[path] = n
    return stats


# --- работа с папкой игры ------------------------------------------------------
OUT = print          # куда писать сообщения (окно подменяет)


def find_game(arg):
    g = Path(arg) if arg else Path.cwd()
    if (g / "Arena.exe").exists():
        return g
    raise SystemExit("Не нашёл Arena.exe. Укажите папку игры: --game \"D:\Total War Arena\"")


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read_state(bdir):
    f = bdir / "state.json"
    if not f.exists():
        return None
    st = json.loads(f.read_text(encoding="utf-8"))
    if "orig" not in st:                      # старый формат: {имя: sha оригинала}
        st = {"orig": st, "patched": {}, "legacy": True}
    return st


def write_state(bdir, st):
    (bdir / "state.json").write_text(json.dumps(st, indent=1), encoding="utf-8")


def has_cyrillic(path):
    pk = Pack(path)
    return any(CYR.search(d.decode("utf-16", "ignore")) for n, d in pk.files if n.endswith(".txt"))


def game_status(game):
    """-> ('clean'|'patched'|'changed'|'missing', подробности)"""
    data, bdir = game / "data", game / "data" / BACKUP_DIR
    for name in EXPECTED:
        if not (data / name).exists():
            return "missing", f"нет файла {name}"
    st = read_state(bdir)
    if st is None:
        return "clean", "бэкап ещё не создан"
    kinds = set()
    for name in EXPECTED:
        cur = sha(data / name)
        kinds.add("orig" if cur == st["orig"].get(name) else
                  "patched" if (cur == st["patched"].get(name) or st.get("legacy")) else "other")
    if kinds == {"patched"}:
        return "patched", "русификатор установлен"
    if kinds == {"orig"}:
        return "clean", "оригинальные файлы"
    if "other" in kinds:
        return "changed", "файлы изменены игрой или другой программой"
    return "patched", "русификатор установлен частично"


def ensure_backup(game, force):
    """Гарантирует бэкап оригиналов. Если игра сама обновила паки, сохраняет новые оригиналы."""
    data, bdir = game / "data", game / "data" / BACKUP_DIR
    st = read_state(bdir)
    names = list(EXPECTED)
    refresh = []
    if st is None:
        refresh = names
    else:
        for name in names:
            cur = sha(data / name)
            if cur not in (st["orig"].get(name), st["patched"].get(name)) and not st.get("legacy"):
                refresh.append(name)
    for name in refresh:
        p = data / name
        if not p.exists():
            raise SystemExit(f"Нет файла {p}")
        if p.stat().st_size != EXPECTED[name] and not force:
            raise SystemExit(f"{name}: размер {p.stat().st_size}, а проверенная сборка клиента даёт {EXPECTED[name]}. "
                             "Это другая версия игры (или файл изменён другой программой). "
                             "Перевод для неё не проверялся; можно продолжить с опцией --force на свой риск.")
        if has_cyrillic(p) and not force:
            raise SystemExit(f"{name}: в паке уже есть кириллица — похоже, он изменён другим русификатором. "
                             "Верните оригинальные файлы (перепроверьте игру в лаунчере) или используйте --force.")
    if refresh:
        bdir.mkdir(parents=True, exist_ok=True)
        st = st or {"orig": {}, "patched": {}}
        for name in refresh:
            shutil.copy2(data / name, bdir / name)
            st["orig"][name] = sha(bdir / name)
            st["patched"].pop(name, None)
        write_state(bdir, st)
        OUT("Оригинальные файлы сохранены в " + str(bdir))
    return bdir


def run_apply(game, tdir, abbr_path=None, force=False, dry=False, csv_path=None, report_only=False, no_abbr=False):
    """Применяет перевод. Возвращает словарь со сводкой."""
    bdir = ensure_backup(game, force)
    if no_abbr or not abbr_path:
        ABBR[1], ABBR[2], ABBR[3], ABBR[4] = [], [], [], []
    else:
        load_abbr(abbr_path)
    tables = load_translations(Path(tdir))
    OUT(f"Загружено таблиц: {len(tables)}, строк: {sum(len(t) for t in tables.values())}")
    log, total, outs = [], 0, {}
    for name in EXPECTED:
        pack = Pack(bdir / name)
        stats = patch_pack(pack, tables, log)
        total += sum(stats.values())
        outs[name] = pack.build()
        assert len(outs[name]) == len(pack.raw)
        OUT(f"{name}: переведено {sum(stats.values())} строк в {len(stats)} файлах")
    skipped = {(r[0].split(BS)[-1], r[1]) for r in log if r[2] == "не влезло"}
    bad = sum(1 for r in log if r[2] == "разметка")
    OUT(f"Не влезло по размеру (остались английскими): {len(skipped)}; отброшено из-за разметки: {bad}")
    if csv_path:
        with Path(csv_path).open("w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f, delimiter=";")
            w.writerow(["файл", "ключ", "причина", "длина_оригинала", "длина_перевода"])
            w.writerows(log)
        OUT(f"Отчёт: {csv_path}")
    if not (dry or report_only):
        try:
            for name, blob in outs.items():
                (game / "data" / name).write_bytes(blob)
        except PermissionError:
            raise SystemExit("Не могу записать пак: закройте игру и лаунчер.")
        st = read_state(bdir)
        for name, blob in outs.items():
            st["patched"][name] = hashlib.sha256(blob).hexdigest()
        write_state(bdir, st)
        OUT("Готово. Запустите игру.")
    return {"translated": total, "skipped": len(skipped), "bad": bad}


def run_restore(game, force=False):
    bdir = game / "data" / BACKUP_DIR
    st = read_state(bdir)
    if st is None:
        raise SystemExit("Бэкап не найден: нечего восстанавливать.")
    status, _ = game_status(game)
    if status == "changed" and not force:
        raise SystemExit("Файлы игры изменены (скорее всего, игра обновилась): старый бэкап больше не подходит. "
                         "Проверьте файлы игры в лаунчере, чтобы получить оригиналы.")
    try:
        for name in EXPECTED:
            shutil.copy2(bdir / name, game / "data" / name)
    except PermissionError:
        raise SystemExit("Не могу записать пак: закройте игру и лаунчер.")
    OUT("Оригинальные паки восстановлены.")


def cmd_apply(a, report_only=False):
    game = find_game(a.game)
    run_apply(game, a.translations, None if a.no_abbr else a.abbr, a.force, a.dry_run,
              a.csv or ("report.csv" if report_only else None), report_only, a.no_abbr)


def cmd_restore(a):
    run_restore(find_game(a.game), a.force)


def cmd_sheet(a):
    game = find_game(a.game)
    bdir = ensure_backup(game, a.force)
    tables = load_translations(Path(a.translations))
    out = Path(a.out); out.mkdir(parents=True, exist_ok=True)
    pack = Pack(bdir / "local_en.pack")
    for path, data in pack.files:
        if not path.endswith(".loc"):
            continue
        _h, rows = loc_read(data)
        t = tables.get(table_name(path), {})
        if not rows:
            continue
        with (out / (table_name(path) + ".csv")).open("w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f, delimiter=";")
            w.writerow(["key", "english", "russian", "dlina_en", "dlina_ru"])
            for k, v, _f in rows:
                ru = t.get(k, "")
                w.writerow([k, v, ru, len(v), len(ru) if ru else ""])
    print(f"Таблицы для переводчиков: {out} (английский текст — ваш, в репозиторий его не коммитьте)")


def main():
    here = Path(__file__).resolve().parent.parent / "translations" / "ru"
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("command", choices=["apply", "restore", "report", "sheet"])
    p.add_argument("--game", help="папка с Arena.exe")
    p.add_argument("--translations", default=str(here), help="папка с *.tsv")
    p.add_argument("--abbr", default=str(here.parent / "abbreviations.tsv"), help="словарь сокращений")
    p.add_argument("--no-abbr", action="store_true", help="не сокращать слова")
    p.add_argument("--dry-run", action="store_true", help="ничего не записывать")
    p.add_argument("--force", action="store_true", help="игнорировать проверки версии")
    p.add_argument("--csv", help="куда сохранить отчёт о пропущенных строках")
    p.add_argument("--out", default="sheets", help="папка для команды sheet")
    a = p.parse_args()
    {"apply": cmd_apply, "restore": cmd_restore, "sheet": cmd_sheet,
     "report": lambda x: cmd_apply(x, report_only=True)}[a.command](a)


if __name__ == "__main__":
    main()
