# === speechbrain_manager.py - 簡化版記憶體管理 ===
import gc
import torch
import threading
import time
import os
import psutil
import logging
from threading import RLock

logger = logging.getLogger(__name__)

class SimpleSpeechBrainManager:
    """簡化的 SpeechBrain 記憶體管理"""
    
    def __init__(self):
        self.model = None
        self.usage_count = 0
        self.max_usage = 3  # 使用3次後重載
        self.lock = RLock()
        self.memory_threshold = 350  # MB
        self.last_used = None
        
    def check_memory(self):
        """檢查記憶體使用情況"""
        try:
            process = psutil.Process(os.getpid())
            memory_mb = process.memory_info().rss / 1024 / 1024
            logger.info(f"💾 當前記憶體使用: {memory_mb:.1f}MB")
            return memory_mb < self.memory_threshold, memory_mb
        except Exception as e:
            logger.warning(f"記憶體檢查失敗: {e}")
            return True, 0
    
    def init_model(self):
        """安全載入模型"""
        if self.model is not None:
            return True
            
        # 檢查記憶體
        memory_ok, memory_mb = self.check_memory()
        if not memory_ok:
            logger.warning(f"⚠️ 記憶體不足，無法載入模型: {memory_mb:.1f}MB")
            return False
            
        try:
            logger.info("🔄 載入 SpeechBrain 模型...")
            from speechbrain.pretrained import SpeakerRecognition
            
            # 嘗試使用預載的模型路徑
            model_paths = [
                "/app/pretrained_models/spkrec",     # Docker 容器路徑
                "pretrained_models/spkrec",          # 本地路徑
                "./pretrained_models/spkrec"         # 相對路徑
            ]
            
            savedir = None
            for path in model_paths:
                if os.path.exists(path):
                    logger.info(f"📁 找到預載模型: {path}")
                    savedir = path
                    break
            
            if not savedir:
                logger.info("📁 使用默認路徑下載模型")
                savedir = "pretrained_models/spkrec"
                os.makedirs(savedir, exist_ok=True)
            
            # 載入模型，強制使用 CPU
            self.model = SpeakerRecognition.from_hparams(
                source="speechbrain/spkrec-ecapa-voxceleb",
                savedir=savedir,
                run_opts={"device": "cpu"}
            )
            
            self.usage_count = 0
            self.last_used = time.time()
            logger.info("✅ SpeechBrain 模型載入成功")
            return True
            
        except Exception as e:
            logger.error(f"❌ 模型載入失敗: {e}")
            self.model = None
            return False
    
    def cleanup_model(self):
        """清理模型釋放記憶體"""
        if self.model is not None:
            logger.info("🧹 清理 SpeechBrain 模型...")
            
            try:
                del self.model
                self.model = None
                self.usage_count = 0
                
                # 強制垃圾回收
                for _ in range(3):
                    gc.collect()
                    time.sleep(0.1)
                
                # 清理 PyTorch 快取
                if torch.cuda.is_available():
                    torch.cuda.empty_cache()
                
                logger.info("✅ 模型記憶體已清理")
                
            except Exception as e:
                logger.error(f"❌ 清理失敗: {e}")
    
    def should_cleanup(self):
        """檢查是否需要清理模型"""
        # 使用次數限制
        if self.usage_count >= self.max_usage:
            return True
            
        # 時間限制 (5分鐘未使用)
        if self.last_used and (time.time() - self.last_used) > 300:
            return True
            
        # 記憶體壓力
        memory_ok, _ = self.check_memory()
        if not memory_ok:
            return True
            
        return False
    
    def compute_similarity(self, audio1_path, audio2_path):
        """計算音頻相似度"""
        with self.lock:
            try:
                # 檢查文件是否存在
                if not (os.path.exists(audio1_path) and os.path.exists(audio2_path)):
                    logger.warning("⚠️ 音頻文件不存在")
                    return 0.65
                
                # 檢查是否需要清理
                if self.should_cleanup():
                    logger.info("🔄 達到使用限制，清理模型")
                    self.cleanup_model()
                
                # 載入模型
                if not self.init_model():
                    logger.warning("⚠️ 模型無法載入，使用預設值")
                    return 0.65
                
                # 執行比較（有超時保護）
                result = [None]
                error = [None]
                finished = [False]
                
                def process():
                    try:
                        score, _ = self.model.verify_files(audio1_path, audio2_path)
                        result[0] = float(score)
                        logger.info(f"🎯 相似度計算完成: {score:.3f}")
                    except Exception as e:
                        error[0] = str(e)
                        logger.error(f"❌ 相似度計算錯誤: {e}")
                    finally:
                        finished[0] = True
                
                # 在新線程中執行，避免阻塞
                thread = threading.Thread(target=process)
                thread.daemon = True
                thread.start()
                
                # 等待結果（最多8秒）
                timeout = 8
                for _ in range(timeout * 2):  # 每0.5秒檢查一次
                    if finished[0]:
                        break
                    time.sleep(0.5)
                
                if not finished[0]:
                    logger.warning("⏰ SpeechBrain 處理超時")
                    self.cleanup_model()
                    return 0.65
                
                if error[0]:
                    logger.warning(f"❌ 處理出錯: {error[0]}")
                    return 0.65
                
                # 更新使用計數
                self.usage_count += 1
                self.last_used = time.time()
                
                if result[0] is not None:
                    # 確保值在合理範圍內
                    score = max(0, min(1, result[0]))
                    logger.info(f"✅ 相似度: {score:.3f} (第{self.usage_count}次使用)")
                    return score
                
                return 0.65
                
            except Exception as e:
                logger.error(f"💥 相似度計算異常: {e}")
                self.cleanup_model()
                return 0.65

# 全局管理器實例
speech_manager = SimpleSpeechBrainManager()

def compute_similarity(audio1_path, audio2_path):
    """主要入口函數 - 替換原有的 compute_similarity"""
    return speech_manager.compute_similarity(audio1_path, audio2_path)

def cleanup_speechbrain():
    """手動清理函數"""
    speech_manager.cleanup_model()

def get_speechbrain_status():
    """獲取狀態信息"""
    memory_ok, memory_mb = speech_manager.check_memory()
    return {
        "model_loaded": speech_manager.model is not None,
        "usage_count": speech_manager.usage_count,
        "max_usage": speech_manager.max_usage,
        "memory_mb": memory_mb,
        "memory_ok": memory_ok,
        "last_used": speech_manager.last_used
    }

# 定期清理線程
def periodic_cleanup():
    """定期檢查和清理"""
    while True:
        time.sleep(300)  # 每5分鐘檢查一次
        try:
            if speech_manager.should_cleanup():
                logger.info("🧹 定期清理觸發")
                speech_manager.cleanup_model()
        except Exception as e:
            logger.error(f"定期清理錯誤: {e}")

# 啟動清理線程
cleanup_thread = threading.Thread(target=periodic_cleanup, daemon=True)
cleanup_thread.start()

logger.info("🚀 SpeechBrain 記憶體管理器已初始化")
