import os
import shutil

def clear_temp_files():
    """
    Скрипт для очистки временных файлов и кэша перед чистым запуском пайплайна.
    """
    # Директории, которые будут рекурсивно удалены
    directories_to_clean = [
        "data/processed",
        "cache"
    ]
    
    print("🧹 Запуск очистки временных файлов и кэша...")
    
    for dir_path in directories_to_clean:
        if os.path.exists(dir_path):
            try:
                shutil.rmtree(dir_path)
                print(f"✅ Полностью удалена директория: {dir_path}")
            except Exception as e:
                print(f"❌ Ошибка при удалении {dir_path}: {e}")
        else:
            print(f"⏭️ Директория не найдена (пропуск): {dir_path}")
            
    # Восстанавливаем базовую структуру, чтобы скриптам не приходилось делать это самим
    os.makedirs("data/processed/osm", exist_ok=True)
    os.makedirs("../data/processed/gis/gis", exist_ok=True)
    os.makedirs("cache", exist_ok=True)
    
    print("✅ Восстановлена базовая структура папок (data/processed/*).")
    print("✨ Очистка успешно завершена! Проект готов к 'чистому' запуску main.py.")

if __name__ == "__main__":
    clear_temp_files()
