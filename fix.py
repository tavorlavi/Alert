def decode_win1252_ps_fallback(s):
    res = bytearray()
    for c in s:
        # If it's a standard win1252 character, reverse map it
        if c == '\u2122': res.append(0x99)
        elif c == '\u0153': res.append(0x9c)
        # add other mappings if needed, or just let python handle it by encoding
        else:
            try:
                b = c.encode('cp1252')
                res.extend(b)
            except UnicodeEncodeError:
                # Undefined in cp1252 (like \x81, \x8d, \x8f, \x90, \x9d)
                res.append(ord(c))
    return res.decode('utf-8')

with open('server.py', 'r', encoding='utf-8') as f:
    text = f.read().replace('\ufeff', '')
    
fixed_text = decode_win1252_ps_fallback(text)
with open('server_fixed.py', 'w', encoding='utf-8') as f:
    f.write(fixed_text)
print("Fixed successfully!")
