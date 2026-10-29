# Каталог подарков

`products.json` содержит 33 карточки с прямыми ссылками на Kaspi и фотографиями
со страниц соответствующих товаров. Импорт обновляет записи по `shop_url`.

29.10.2026 добавлены 15 товаров для видеосъёмки, аудио и ПК: два дрона DJI,
две петлички BOYA (USB-C и Lightning), Fifine AM8, Sony WH-CH520,
HyperX Cloud III, свет и штатив Ulanzi, DJI Osmo Mobile 6, веб-камера Logitech,
клавиатура AULA, две мыши Logitech и USB-C хаб Ugreen.
Цены прочитаны с карточек Kaspi для Алматы; цена и наличие могут меняться.
Ссылки на источники и фотографии хранятся в каждой записи.
Дроны отнесены к подаркам для взрослых (18+) как редакционное правило каталога.
Теги `видеосъёмка`, `пк`, `дроны` доступны в выборе интересов; общий тег
`гаджеты` сохранён для существующих профилей.

Проверка без изменения базы:

```bash
python scripts/import_gifts.py catalog/products.json
python scripts/check_catalog_images.py
```

Импорт в настроенную через `DATABASE_URL` базу:

```bash
python scripts/import_gifts.py catalog/products.json --apply
```
