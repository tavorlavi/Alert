def fix_mangled_utf8(text):
    out = []
    i = 0
    while i < len(text):
        c = text[i]
        # Is it part of a mangled win1252 sequence?
        # Mangled win-1252 to utf8 looks like chars mostly in Latin-1 Supplement (0x80-0xFF)
        # or mapped chars like \u2122, \u0153 etc.
        # But wait, original Hebrew in UTF-8:
        # e.g. Aleph 'א' = U+05D0 -> UTF-8 bytes 0xD7 0x90.
        # Mangled, PowerShell read 0xD7 as '×' (U+00D7), and 0x90 as '\x90' (U+0090).
        # Which became the string "×\x90" (lengths 2).
        if c == '\u00D7' and i + 1 < len(text):
            n = text[i+1]
            try:
                # reconstruct original bytes
                b1 = 0xD7
                # check what the second char is:
                if n == '\u2122': b2 = 0x99
                elif n == '\u0153': b2 = 0x9C
                elif n == '\u201C': b2 = 0x93 # left double quote
                elif n == '\u201D': b2 = 0x94 # right double quote
                elif n == '\u2022': b2 = 0x95 # bullet
                elif n == '\u2013': b2 = 0x96 # en dash
                elif n == '\u2014': b2 = 0x97 # em dash
                elif n == '\u02DC': b2 = 0x98 # small tilde
                elif n == '\u0161': b2 = 0x9A # s caron
                elif n == '\u203A': b2 = 0x9B # right pointing single guillemet
                elif n == '\u017E': b2 = 0x9E # z caron
                elif n == '\u0178': b2 = 0x9F # Y diaeresis
                else:
                    try:
                        b2 = n.encode('cp1252')[0]
                    except UnicodeEncodeError:
                        b2 = ord(n)
                
                # Now we have b1 and b2. Decode them as utf-8!
                char_bytes = bytes([b1, b2])
                try:
                    hebrew_char = char_bytes.decode('utf-8')
                    out.append(hebrew_char)
                    i += 2
                    continue
                except UnicodeDecodeError:
                    pass
            except Exception as e:
                pass
                
        out.append(c)
        i += 1
    return "".join(out)

with open('server.py', 'r', encoding='utf-8') as f:
    text = f.read().replace('\ufeff', '')

fixed = fix_mangled_utf8(text)
with open('server_fixed.py', 'w', encoding='utf-8') as f:
    f.write(fixed)
print("Done")
