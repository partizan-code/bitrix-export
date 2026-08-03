import os

list_files = os.listdir('export/deals')
text_result = ''

for file in list_files:
    try:
        with open(file=f'export/deals/{file}', mode='r', encoding='utf-8') as f:
            text_result += f"\n{f.read()}"
    except UnicodeDecodeError as e:
        print(f"Пропускаем файл: {file} (ошибка: {e})")
        continue

with open(file=f'export/deals/result.md', mode='w', encoding='utf-8') as f:
    f.write(text_result)
