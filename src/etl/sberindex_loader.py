import pandas as pd
import os
from typing import Dict, Optional, Tuple

class SberIndexLoader:
    """
    Класс для загрузки и обработки данных СберИндекса о внутреннем туризме.
    Обеспечивает расчет сезонных пиков и глобальных трендов для калибровки 
    индексов привлекательности и экологической емкости модели.
    """

    def __init__(self, data_dir: str = "data/processed/gis"):
        self.data_dir = data_dir
        self.filename = "vnutrennikh-turistov_ru.csv"

    def _load_data(self) -> Optional[pd.DataFrame]:
        """
        Загружает CSV файл с данными СберИндекса.
        """
        filepath = os.path.join(self.data_dir, self.filename)
        if not os.path.exists(filepath):
            print(f"Внимание: файл СберИндекса не найден по пути {filepath}.")
            return None

        try:
            # Читаем CSV, разделитель - точка с запятой, конвертируем даты
            df = pd.read_csv(filepath, sep=';')
            df['period'] = pd.to_datetime(df['period'])
            df['value'] = pd.to_numeric(df['value'], errors='coerce')
            return df
        except Exception as e:
            print(f"Ошибка при чтении {filepath}: {e}")
            return None

    def get_regional_trend(self, region_name: str = "Ленинградская область") -> Optional[float]:
        """
        Рассчитывает средний тренд (% г/г) для заданного региона 
        за последний доступный год или за весь период.
        Можно использовать как глобальный множитель (Global Multiplier).
        """
        df = self._load_data()
        if df is None:
            return None

        region_df = df[df['ref_area'] == region_name].copy()
        if region_df.empty:
            print(f"Данные для региона '{region_name}' не найдены.")
            return None

        # Сортируем по времени и берем последний год для более актуального тренда
        region_df = region_df.sort_values(by='period')
        
        # Если данных мало, берем среднее за весь период, иначе за последние 12 месяцев
        if len(region_df) >= 12:
            recent_trend = region_df['value'].tail(12).mean()
        else:
            recent_trend = region_df['value'].mean()
            
        return float(recent_trend)

    def get_seasonality_profile(self, region_name: str = "Ленинградская область") -> Optional[Dict[int, float]]:
        """
        Рассчитывает профиль сезонности по месяцам.
        Возвращает словарь, где ключ - номер месяца (1-12), 
        а значение - усредненный показатель (% г/г) для этого месяца.
        """
        df = self._load_data()
        if df is None:
            return None

        region_df = df[df['ref_area'] == region_name].copy()
        if region_df.empty:
            return None

        # Добавляем колонку с месяцем
        region_df['month'] = region_df['period'].dt.month
        
        # Группируем по месяцам и считаем среднее
        monthly_avg = region_df.groupby('month')['value'].mean().to_dict()
        
        return monthly_avg

    def calculate_capacity_multiplier(self, region_name: str = "Ленинградская область") -> float:
        """
        Рассчитывает множитель нагрузки (Capacity Multiplier) на основе пикового месяца 
        относительно среднегодового значения. 
        Если пик значительно превышает среднее, возвращает множитель > 1.0.
        Полезно для оценки инфраструктуры на пиковую нагрузку.
        """
        seasonality = self.get_seasonality_profile(region_name)
        if not seasonality:
            return 1.0 # Базовый множитель, если данных нет
            
        avg_value = sum(seasonality.values()) / len(seasonality)
        peak_value = max(seasonality.values())
        
        # Защита от деления на ноль или отрицательных средних
        # В случае с % г/г, базовое значение - это 100% (то есть 0 в терминах отклонения)
        # Для простоты, мы нормализуем значения: 
        # Если пик больше среднего, увеличиваем инфраструктуру
        
        if avg_value > 0 and peak_value > avg_value:
             # Насколько пик превышает среднее (в долях)
             multiplier = 1.0 + ((peak_value - avg_value) / 100.0) 
        else:
             multiplier = 1.0
             
        # Ограничим множитель разумными пределами, например от 1.0 до 3.0
        return max(1.0, min(float(multiplier), 3.0))

# Пример использования (можно раскомментировать для проверки)
# if __init__ == "__main__":
#     loader = SberIndexLoader(data_dir="../../data/processed/gis")
#     trend = loader.get_regional_trend()
#     seasonality = loader.get_seasonality_profile()
#     capacity_mult = loader.calculate_capacity_multiplier()
#
#     print(f"Тренд Ленобласти: {trend}% г/г")
#     print(f"Множитель пиковой нагрузки: {capacity_mult}")
