import os
import pandas as pd
from sqlalchemy import create_engine, text
from catboost import CatBoostRegressor
from sklearn.metrics import mean_absolute_percentage_error, root_mean_squared_error
from dotenv import load_dotenv

load_dotenv()
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql+psycopg://postgres:postgres@localhost:5433/deals_bigdata")
engine = create_engine(DATABASE_URL)


def run_ml_pipeline():
    print("Запуск ML-пайплайна прогнозирования тарифов (CatBoost)...")

    # 1. Загрузка данных из Core DWH (только чистые факты)
    query = """
        SELECT 
            p.id,
            pd.price_date AS target_date,
            p.loading_date,
            p.loading_city_gcid,
            p.unloading_city_gcid,
            p.distance,
            p.total_weight,
            p.total_volume,
            p.body_type,
            p.weight_category,
            p.volume_category,
            p.price_rub,
            (p.price_rub / NULLIF(p.distance, 0)) AS price_per_km
        FROM core.cleaned_prices p
        JOIN price_dates pd ON p.id = pd.price_id
        WHERE p.is_fact = true 
          AND p.complex = false 
          AND p.outlier_mark = false
          AND p.price_rub > 0 
          AND p.distance > 0
          AND pd.price_date <= '2026-10-26';
    """

    with engine.connect() as conn:
        df = pd.read_sql(query, conn)

    print(f"Загружено строк для ML: {len(df)}")

    # 2. Инжиниринг фичей (Feature Engineering)
    df['target_date'] = pd.to_datetime(df['target_date'])
    df['month'] = df['target_date'].dt.month
    df['day_of_week'] = df['target_date'].dt.day_of_week

    # Заполняем пропуски в категориальных фичах
    df['body_type'] = df['body_type'].fillna(-1).astype(str)
    df['weight_category'] = df['weight_category'].fillna('OTH')
    df['volume_category'] = df['volume_category'].fillna('OTH')

    # Выделяем фичи (X) и таргет (y — ставка за км для устойчивости к разной длине плеча)
    features = [
        'loading_city_gcid', 'unloading_city_gcid',
        'distance', 'total_weight', 'total_volume',
        'body_type', 'weight_category', 'volume_category',
        'month', 'day_of_week'
    ]

    target = 'price_per_km'

    # Очищаем от возможных NaN в таргете
    df = df.dropna(subset=[target])

    # 3. Хронологический сплит (Time-based split без утечек)
    # Обучение: июль – август 2026
    # Валидация: сентябрь 2026
    # Тест: октябрь 2026
    train_df = df[df['target_date'] < '2026-09-01']
    val_df = df[(df['target_date'] >= '2026-09-01') & (df['target_date'] < '2026-10-01')]
    test_df = df[df['target_date'] >= '2026-10-01']

    X_train, y_train = train_df[features], train_df[target]
    X_val, y_val = val_df[features], val_df[target]
    X_test, y_test = test_df[features], test_df[target]

    print(ж := f"Сплит данных -> Train: {len(X_train)} | Val: {len(X_val)} | Test: {len(X_test)}")

    cat_features = ['loading_city_gcid', 'unloading_city_gcid', 'body_type', 'weight_category', 'volume_category']

    # 4. Инициализация и обучение CatBoostRegressor
    model = CatBoostRegressor(
        iterations=500,
        learning_rate=0.05,
        depth=6,
        loss_function='RMSE',
        eval_metric='MAPE',
        random_seed=42,
        verbose=100
    )

    model.fit(
        X_train, y_train,
        eval_set=(X_val, y_val),
        cat_features=cat_features,
        early_stopping_rounds=50
    )

    # 5. Оценка качества на тестовом периоде (Октябрь)
    preds = model.predict(X_test)
    mape = mean_absolute_percentage_error(y_test, preds)
    rmse = root_mean_squared_error(y_test, preds)

    print(f"\nРезультаты модели на тестовой выборке (Октябрь):")
    print(f"   - MAPE: {mape * 100:.2f}%")
    print(f"   - RMSE: {rmse:.2f} руб./км")

    # 6. Сохранение предсказаний с доверительным интервалом (p25 / p75 коридор)
    # Для построения коридора используем квантильную регрессию в CatBoost или прокси по отклонениям дерева
    test_df['predicted_price_per_km'] = preds
    test_df['predicted_trip_price'] = preds * test_df['distance']

    # Доверительный интервал (коридор ± отклонение на основе исторической ошибки)
    error_margin = preds * mape
    test_df['corridor_lower'] = (preds - error_margin) * test_df['distance']
    test_df['corridor_upper'] = (preds + error_margin) * test_df['distance']

    # Сохраняем витрину прогнозов в базу данных
    with engine.begin() as conn:
        conn.execute(text("CREATE SCHEMA IF NOT EXISTS ml;"))
        test_df[['id', 'target_date', 'predicted_trip_price', 'corridor_lower', 'corridor_upper']].to_sql(
            'ml_tariff_predictions', conn, schema='ml', if_exists='replace', index=False
        )

    print("ML-пайплайн успешно завершен, прогнозы и коридоры сохранены в схему ml.ml_tariff_predictions!")


if __name__ == "__main__":
    run_ml_pipeline()