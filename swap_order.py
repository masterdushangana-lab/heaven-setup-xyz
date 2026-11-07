with open('subsplease_episodes.py') as f:
    text = f.read()

# Swap Section 1 and Section 2 in subsplease_episodes.py
# Let's inspect how Section 1 and 2 are laid out
s1_start = text.find('# 1. Fetch & Process SubsPlease Releases')
s2_start = text.find('# 2. Fetch & Process Erai-raws Releases')
footer_start = text.find('print(f"\\n[*] Total Selected Episodes to Ingest:')

assert s1_start != -1 and s2_start != -1 and footer_start != -1

head = text[:s1_start]
s1_block = text[s1_start:s2_start]
s2_block = text[s2_start:footer_start]
footer = text[footer_start:]

# Rename headers
s2_block_renamed = s2_block.replace('# 2. Fetch & Process Erai-raws Releases (Zero Duplicates!)', '# 1. Fetch & Process Erai-raws Releases (Accurate Part & Cour Detection)')
s1_block_renamed = s1_block.replace('# 1. Fetch & Process SubsPlease Releases', '# 2. Fetch & Process SubsPlease Releases (Complementary Fresh Releases)')

# Compose in new order: Erai-raws first, then SubsPlease
new_text = head + s2_block_renamed + s1_block_renamed + footer

with open('subsplease_episodes.py', 'w') as f:
    f.write(new_text)

print('Swapped order in subsplease_episodes.py: Erai-raws now runs first!')
