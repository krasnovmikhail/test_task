import os
import pandas as pd
from sqlalchemy import create_engine, text
from dotenv import load_dotenv

# Загружаем переменные окружения из .env
load_dotenv()
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql+psycopg://postgres:postgres@localhost:5433/deals_bigdata")

engine = create_engine(DATABASE_URL)


def run_etl(start_date: str = '2026-07-01', end_date: str = '2026-10-26'):
    print(f"Запуск ETL-пайплайна за период с {start_date} по {end_date}...")

    with engine.connect() as conn:
        # 1. Чтение сырых данных из prices с учетом фильтра по окну дат погрузки
        query_prices = f"""
            SELECT id, loading_date, loading_city_gcid, unloading_city_gcid, 
                   distance, total_weight, total_volume, body_type, 
                   price, currency_code, price_type, is_fact, complex, 
                   nearest_rate_date, outlier_mark
            FROM prices
            WHERE loading_date >= '{start_date}' AND loading_date <= '{end_date}';
        """
        df_prices = pd.read_sql(query_prices, conn)

        # Загружаем справочники (для городов берем уникальные gc_id с минимальным id, чтобы избавиться от дублей)
        df_cities = pd.read_sql("SELECT DISTINCT ON (gc_id) gc_id FROM cities ORDER BY gc_id, id;", conn)
        df_rates = pd.read_sql("SELECT num_code, setting_date, value FROM exchange_rates;", conn)

    print(f"Загружено сырых строк: {len(df_prices)}")

    # 2. Валидация и очистка (Data Quality & Cleansing)
    valid_cities = set(df_cities['gc_id'])

    def validate_row(row):
        if row['price'] <= 0:
            return "INVALID_PRICE_ZERO_OR_NEGATIVE"
        if str(row['currency_code']) == '643' and row['price'] <= 1000:
            return "INVALID_RUB_PRICE_TOO_LOW"
        if row['distance'] <= 0:
            return "INVALID_DISTANCE"
        if row['total_weight'] < 0 or row['total_volume'] < 0:
            return "INVALID_WEIGHT_OR_VOLUME"
        if row['loading_city_gcid'] not in valid_cities or row['unloading_city_gcid'] not in valid_cities:
            return "CITY_NOT_FOUND_IN_DICTIONARY"
        return "VALID"

    df_prices['dq_status'] = df_prices.apply(validate_row, axis=1)

    # Разделяем на валидные и первичный карантин
    df_valid = df_prices[df_prices['dq_status'] == "VALID"].copy()
    df_quarantine = df_prices[df_prices['dq_status'] != "VALID"].copy()

    # 3. Конвертация валют в RUB
    df_rates['setting_date'] = pd.to_datetime(df_rates['setting_date']).dt.date
    df_valid['nearest_rate_date'] = pd.to_datetime(df_valid['nearest_rate_date']).dt.date

    def get_exchange_rate(row):
        curr = str(row['currency_code'])
        if curr == '643':
            return 1.0

        rate_date = row['nearest_rate_date']
        if pd.isna(rate_date):
            return None

        match = df_rates[(df_rates['num_code'] == curr) & (df_rates['setting_date'] == rate_date)]
        if not match.empty:
            return float(match['value'].values[0])
        return None

    df_valid['rate_val'] = df_valid.apply(get_exchange_rate, axis=1)

    # Отправляем в карантин сделки, у которых не нашелся курс иностранной валюты
    no_rate_mask = df_valid['rate_val'].isna() & (df_valid['currency_code'].astype(str) != '643')
    df_no_rate = df_valid[no_rate_mask].copy()
    df_no_rate['dq_status'] = "EXCHANGE_RATE_NOT_FOUND"

    df_quarantine = pd.concat([df_quarantine, df_no_rate], ignore_index=True)
    df_valid = df_valid[~no_rate_mask].copy()

    # Рассчитываем итоговую цену в рублях
    df_valid['price_rub'] = df_valid['price'] * df_valid['rate_val']

    # 4. Обогащение категорий веса и объема
    def assign_weight_category(w):
        if w >= 20.0:
            return '20T'
        elif w >= 10.0:
            return '10T'
        elif w >= 5.0:
            return '5T'
        elif w > 0:
            return 'L5T'
        return 'OTH'

    def assign_volume_category(v):
        if v >= 82.0: return 'FTL'
        return 'OTH'

    df_valid['weight_category'] = df_valid['total_weight'].apply(assign_weight_category)
    df_valid['volume_category'] = df_valid['total_volume'].apply(assign_volume_category)

    print(f"Прошло валидацию: {len(df_valid)} строк | Отправлено в карантин: {len(df_quarantine)} строк")

    # 5. Идемпотентная загрузка в DWH (Core & Quarantine таблицы)
    with engine.begin() as conn:
        # Создаем структуру схем и таблиц при необходимости
        conn.execute(text("""
            CREATE SCHEMA IF NOT EXISTS core;

            CREATE TABLE IF NOT EXISTS core.cleaned_prices (
                id INT PRIMARY KEY,
                loading_date DATE,
                loading_city_gcid INT,
                unloading_city_gcid INT,
                distance NUMERIC,
                total_weight NUMERIC,
                total_volume NUMERIC,
                body_type INT,
                price NUMERIC,
                currency_code VARCHAR(3),
                price_type INT,
                is_fact BOOLEAN,
                complex BOOLEAN,
                outlier_mark BOOLEAN,
                price_rub NUMERIC,
                weight_category VARCHAR(10),
                volume_category VARCHAR(10),
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS public.dq_quarantine_prices (
                id INT,
                loading_date DATE,
                price NUMERIC,
                currency_code VARCHAR(3),
                dq_reason VARCHAR(100),
                logged_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """))

        # Очищаем данные за этот же период для идемпотентности (пересчет без задвоения)
        conn.execute(text(
            f"DELETE FROM core.cleaned_prices WHERE loading_date >= '{start_date}' AND loading_date <= '{end_date}';"))

    # Сохраняем очищенные данные в core.cleaned_prices
    cols_to_save = [
        'id', 'loading_date', 'loading_city_gcid', 'unloading_city_gcid',
        'distance', 'total_weight', 'total_volume', 'body_type',
        'price', 'currency_code', 'price_type', 'is_fact', 'complex',
        'outlier_mark', 'price_rub', 'weight_category', 'volume_category'
    ]

    with engine.begin() as conn:
        df_valid[cols_to_save].to_sql('cleaned_prices', conn, schema='core', if_exists='append', index=False,
                                      method='multi', chunksize=500)

        # Сохраняем карантинные строки
        if not df_quarantine.empty:
            df_q_save = df_quarantine[['id', 'loading_date', 'price', 'currency_code', 'dq_status']].copy()
            df_q_save = df_q_save.rename(columns={'dq_status': 'dq_reason'})
            df_q_save.to_sql('dq_quarantine_prices', conn, schema='public', if_exists='append', index=False,
                             method='multi', chunksize=500)

    print("ETL-пайплайн успешно завершен и данные загружены в Core DWH!")


if __name__ == "__main__":
    run_etl()