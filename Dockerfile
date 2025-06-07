# === 優化的 Dockerfile - 預載 SpeechBrain 模型 ===
FROM python:3.11-slim

# 設置工作目錄
WORKDIR /app

# 安裝系統依賴
RUN apt-get update && apt-get install -y \
    gcc \
    g++ \
    libsndfile1 \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# 複製 requirements
COPY requirements.txt .

# 安裝 Python 依賴
RUN pip install --no-cache-dir -r requirements.txt

# === 關鍵步驟：預先下載 SpeechBrain 模型 ===
RUN python -c "
import os
os.makedirs('pretrained_models/spkrec', exist_ok=True)

try:
    print('🔄 Downloading SpeechBrain model...')
    from speechbrain.pretrained import SpeakerRecognition
    model = SpeakerRecognition.from_hparams(
        source='speechbrain/spkrec-ecapa-voxceleb',
        savedir='pretrained_models/spkrec'
    )
    print('✅ SpeechBrain model downloaded successfully')
    
    # 測試模型載入
    print('🧪 Testing model loading...')
    del model  # 釋放記憶體
    print('✅ Model test completed')
    
except Exception as e:
    print(f'⚠️ Model download failed: {e}')
    print('ℹ️ Application will use fallback methods')
"

# 複製應用程式代碼
COPY . .

# 設置環境變數
ENV PYTHONPATH=/app
ENV SPEECHBRAIN_CACHE_DIR=/app/pretrained_models

# 暴露端口
EXPOSE 5000

# 啟動命令
CMD ["gunicorn", "thai_learning:app", "--bind", "0.0.0.0:5000", "--workers", "1", "--timeout", "120"]
