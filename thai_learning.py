import os
import uuid
import random
import json
from datetime import datetime, timedelta
import io
import numpy as np
from pydub import AudioSegment
import requests
import logging
from dotenv import load_dotenv

from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import (
    MessageEvent, TextMessage, AudioMessage, ImageMessage,
    TextSendMessage, ImageSendMessage, AudioSendMessage,
    TemplateSendMessage, ButtonsTemplate, MessageAction,
    URIAction, QuickReply, QuickReplyButton
)
import azure.cognitiveservices.speech as speechsdk

# 設置日誌
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# 加載環境變數
load_dotenv()  # 載入 .env 文件中的環境變數 (本地開發用)

# === 應用初始化 ===
app = Flask(__name__)

# LINE Bot設定
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN', 'YOUR_CHANNEL_ACCESS_TOKEN')
LINE_CHANNEL_SECRET = os.environ.get('LINE_CHANNEL_SECRET', 'YOUR_CHANNEL_SECRET')

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# Azure Speech Services設定
speech_key = os.environ.get('AZURE_SPEECH_KEY', 'YOUR_AZURE_SPEECH_KEY')
speech_region = os.environ.get('AZURE_SPEECH_REGION', 'eastasia')

logger.info(f"初始化應用程式... LINE Bot 和 Azure Speech 服務已配置")

# === 用戶數據管理 ===
class UserData:
    def __init__(self):
        self.users = {}
        # 在實際應用中，應該使用資料庫存儲這些數據
        logger.info("初始化用戶數據管理器")
        
    def get_user_data(self, user_id):
        """獲取用戶數據，如果不存在則初始化"""
        if user_id not in self.users:
            logger.info(f"為新用戶創建數據: {user_id}")
            self.users[user_id] = {
                'score': 0,
                'current_activity': None,
                'current_vocab': None,
                'current_category': None,
                'game_state': {},
                'vocab_mastery': {},
                'learning_progress': {},
                'last_active': self.current_date(),
                'streak': 0
            }
        return self.users[user_id]
    
    def current_date(self):
        """獲取當前日期，便於追蹤學習進度"""
        return datetime.now().strftime("%Y-%m-%d")
    
    def update_streak(self, user_id):
        """更新用戶的連續學習天數"""
        user_data = self.get_user_data(user_id)
        last_active = datetime.strptime(user_data['last_active'], "%Y-%m-%d")
        today = datetime.now()
        
        if (today - last_active).days == 1:  # 連續下一天學習
            user_data['streak'] += 1
            logger.info(f"用戶 {user_id} 連續學習天數增加到 {user_data['streak']} 天")
        elif (today - last_active).days > 1:  # 中斷了連續學習
            user_data['streak'] = 1
            logger.info(f"用戶 {user_id} 連續學習中斷，重置為 1 天")
        # 如果是同一天，streak保持不變
        
        user_data['last_active'] = self.current_date()

user_data_manager = UserData()
# === 輔助函數 ===
def get_audio_content(message_id):
    """從LINE取得音訊內容"""
    logger.info(f"獲取音訊內容，訊息ID: {message_id}")
    message_content = line_bot_api.get_message_content(message_id)
    audio_content = b''
    for chunk in message_content.iter_content():
        audio_content += chunk
    return audio_content

def process_audio_content(audio_content):
    """處理音頻內容並轉換為適合語音識別的格式"""
    try:
        # 創建臨時目錄
        temp_dir = os.environ.get('TEMP', '/tmp')
        audio_dir = os.path.join(temp_dir, 'temp_audio')
        os.makedirs(audio_dir, exist_ok=True)
        
        # 生成唯一的文件名
        temp_m4a = os.path.join(audio_dir, f'temp_{uuid.uuid4()}.m4a')
        temp_wav = os.path.join(audio_dir, f'temp_{uuid.uuid4()}.wav')
        
        logger.info(f"保存原始音頻到 {temp_m4a}")
        # 保存原始音頻
        with open(temp_m4a, 'wb') as f:
            f.write(audio_content)
        
        logger.info("使用 pydub 轉換音頻格式")
        # 使用 pydub 轉換格式
        audio = AudioSegment.from_file(temp_m4a)
        audio = audio.set_frame_rate(16000).set_channels(1)
        audio.export(temp_wav, format='wav')
        
        # 清除原始文件
        try:
            os.remove(temp_m4a)
            logger.info(f"已清除臨時文件 {temp_m4a}")
        except Exception as e:
            logger.warning(f"清除臨時文件失敗: {str(e)}")
            pass
            
        return temp_wav
    except Exception as e:
        logger.error(f"音頻處理錯誤: {str(e)}")
        # 如果處理失敗，返回原始音頻
        temp_path = os.path.join(audio_dir, f'original_{uuid.uuid4()}.bin')
        with open(temp_path, 'wb') as f:
            f.write(audio_content)
        return temp_path

def evaluate_pronunciation(audio_file_path, reference_text, language="th-TH"):
    """使用Azure Speech Services進行發音評估"""
    try:
        logger.info(f"開始發音評估，參考文本: {reference_text}")
        # 設定語音配置
        speech_config = speechsdk.SpeechConfig(subscription=speech_key, region=speech_region)
        speech_config.speech_recognition_language = language
        
        # 設定發音評估配置
        pronunciation_config = speechsdk.PronunciationAssessmentConfig(
            reference_text=reference_text,
            grading_system=speechsdk.PronunciationAssessmentGradingSystem.HundredMark,
            granularity=speechsdk.PronunciationAssessmentGranularity.Phoneme,
            enable_miscue=True
        )
        
        # 設定音訊輸入
        audio_config = speechsdk.audio.AudioConfig(filename=audio_file_path)
        
        # 創建語音識別器
        speech_recognizer = speechsdk.SpeechRecognizer(
            speech_config=speech_config, 
            audio_config=audio_config
        )
        
        # 應用發音評估配置
        pronunciation_assessment = pronunciation_config.apply_to(speech_recognizer)
        
        # 開始識別
        logger.info("開始識別語音...")
        result = speech_recognizer.recognize_once_async().get()
        
        # 處理結果
        if result.reason == speechsdk.ResultReason.RecognizedSpeech:
            pronunciation_result = speechsdk.PronunciationAssessmentResult(result)
            
            # 獲取評估結果
            accuracy_score = pronunciation_result.accuracy_score
            pronunciation_score = pronunciation_result.pronunciation_score
            completeness_score = pronunciation_result.completeness_score
            fluency_score = pronunciation_result.fluency_score
            
            # 計算總分
            overall_score = int((accuracy_score + pronunciation_score + completeness_score + fluency_score) / 4)
            
            logger.info(f"發音評估完成，總分: {overall_score}")
            return {
                "success": True,
                "recognized_text": result.text,
                "reference_text": reference_text,
                "overall_score": overall_score,
                "accuracy_score": accuracy_score,
                "pronunciation_score": pronunciation_score,
                "completeness_score": completeness_score,
                "fluency_score": fluency_score
            }
        else:
            logger.warning(f"語音識別失敗，原因: {result.reason}")
            return {
                "success": False,
                "error": "無法識別語音",
                "result_reason": result.reason
            }
    
    except Exception as e:
        logger.error(f"發音評估過程中發生錯誤: {str(e)}")
        return {
            "success": False,
            "error": str(e)
        }
    finally:
        # 清理臨時檔案
        if os.path.exists(audio_file_path):
            try:
                os.remove(audio_file_path)
                logger.info(f"已清除臨時檔案 {audio_file_path}")
            except Exception as e:
                logger.warning(f"清除臨時檔案失敗: {str(e)}")
                pass
            # === 翻牌配對遊戲功能 ===
def init_matching_game(user_id, category=None):
    """初始化記憶翻牌遊戲"""
    logger.info(f"初始化記憶翻牌遊戲，用戶ID: {user_id}")
    user_data = user_data_manager.get_user_data(user_id)
    user_data['current_activity'] = 'matching_game'
    
    # 選擇詞彙類別
    if category:
        cat_words = thai_data['categories'][category]['words']
    else:
        # 預設使用當前類別或隨機類別
        current_cat = user_data.get('current_category')
        if current_cat:
            cat_words = thai_data['categories'][current_cat]['words']
        else:
            # 隨機選一個類別
            random_cat = random.choice(list(thai_data['categories'].keys()))
            cat_words = thai_data['categories'][random_cat]['words']
    
    # 選擇6個詞彙製作12張牌（中文和泰文各一張）
    selected_words = random.sample(cat_words, min(6, len(cat_words)))
    
    # 創建牌組
    cards = []
    for word in selected_words:
        thai_word = thai_data['basic_words'][word]['thai']
        # 添加中文卡
        cards.append({
            'id': f'zh_{word}',
            'value': word,
            'type': 'zh',
            'matched': False,
            'flipped': False
        })
        # 添加泰文卡
        cards.append({
            'id': f'th_{word}',
            'value': thai_word,
            'type': 'th',
            'matched': False,
            'flipped': False
        })
    
    # 洗牌
    random.shuffle(cards)
    
    # 保存遊戲狀態
    user_data['game_state'] = {
        'cards': cards,
        'flipped_cards': [],
        'matched_pairs': 0,
        'total_pairs': len(selected_words),
        'moves': 0,
        'start_time': datetime.now().isoformat()
    }
    
    return user_data['game_state']

def handle_card_flip(user_id, card_id):
    """處理卡片翻轉邏輯"""
    logger.info(f"處理卡片翻轉，用戶ID: {user_id}，卡片ID: {card_id}")
    user_data = user_data_manager.get_user_data(user_id)
    game_state = user_data.get('game_state', {})
    
    if not game_state:
        return None, "請先開始遊戲"
    
    # 找出對應的卡片
    card = None
    for c in game_state['cards']:
        if c['id'] == card_id:
            card = c
            break
    
    if not card:
        return game_state, "無效的卡片ID"
    
    if card['matched']:
        return game_state, "此卡片已配對成功"
    
    flipped_cards = game_state['flipped_cards']
    
    # 如果已經翻了兩張牌，先重置
    if len(flipped_cards) >= 2:
        # 重置未配對的翻牌
        for c in game_state['cards']:
            if c['id'] in flipped_cards and not c['matched']:
                c['flipped'] = False
        flipped_cards = []
    
    # 翻轉當前卡片
    card['flipped'] = True
    flipped_cards.append(card_id)
    game_state['flipped_cards'] = flipped_cards
    game_state['moves'] += 1
    
    # 檢查是否配對成功
    if len(flipped_cards) == 2:
        card1 = None
        card2 = None
        for c in game_state['cards']:
            if c['id'] == flipped_cards[0]:
                card1 = c
            elif c['id'] == flipped_cards[1]:
                card2 = c
        
        # 檢查是否為一對（中文與對應泰文）
        if card1 and card2:
            word1 = card1['id'].split('_')[1]
            word2 = card2['id'].split('_')[1]
            
            # 如果是同一個詞的不同語言版本
            is_match = False
            
            # 檢查一：兩張卡片類型不同
            if card1['type'] != card2['type']:
                # 檢查二：如果一張是中文一張是泰文
                if (card1['type'] == 'zh' and card2['type'] == 'th'):
                    # 檢查中文卡的值是否對應泰文卡
                    if card2['value'] == thai_data['basic_words'].get(card1['value'], {}).get('thai'):
                        is_match = True
                elif (card1['type'] == 'th' and card2['type'] == 'zh'):
                    # 檢查泰文卡的值是否對應中文卡
                    if card1['value'] == thai_data['basic_words'].get(card2['value'], {}).get('thai'):
                        is_match = True
            
            if is_match:
                card1['matched'] = True
                card2['matched'] = True
                game_state['matched_pairs'] += 1
                result_message = "配對成功！"
            else:
                result_message = "配對不符，請再試一次"
        else:
            result_message = "發生錯誤，請重試"
    else:
        result_message = "請選擇第二張卡片"
    
    # 檢查遊戲是否結束
    if game_state['matched_pairs'] >= game_state['total_pairs']:
        # 計算得分（基於移動次數和時間）
        end_time = datetime.now()
        start_time = datetime.fromisoformat(game_state['start_time'])
        duration = (end_time - start_time).total_seconds()
        
        # 基礎分數 + 時間獎勵 + 移動次數獎勵
        base_score = 1000
        time_bonus = max(0, 300 - duration) * 2  # 最快時間獎勵
        move_bonus = max(0, 100 - (game_state['moves'] - game_state['total_pairs']) * 5)  # 最少移動獎勵
        
        total_score = int(base_score + time_bonus + move_bonus)
        game_state['score'] = total_score
        
        # 更新用戶總分
        user_data['score'] += total_score
        
        result_message = f"恭喜！遊戲完成！\n得分：{total_score}\n用時：{int(duration)}秒\n移動次數：{game_state['moves']}"
    
    return game_state, result_message

def create_matching_game_message(game_state):
    """創建記憶翻牌遊戲界面"""
    logger.info("創建翻牌遊戲界面")
    # 遊戲狀態訊息
    status_message = TextSendMessage(
        text=f"記憶翻牌遊戲\n已配對：{game_state['matched_pairs']}/{game_state['total_pairs']}\n移動次數：{game_state['moves']}"
    )
    
    # 創建卡片選單（分成多個按鈕模板，每個最多4個按鈕）
    card_messages = []
    cards = game_state['cards']
    
    # 每4張卡片一組
    for i in range(0, len(cards), 4):
        card_group = cards[i:i+4]
        actions = []
        
        for card in card_group:
            if card['matched']:
                # 已配對的卡片顯示實際內容
                label = f"✓ {card['value']}"
            elif card.get('flipped'):
                # 已翻開但未配對的卡片
                label = f"📖 {card['value']}"
            else:
                # 未翻開的卡片
                label = f"🔒 卡片 {i + card_group.index(card) + 1}"
            
            # 只有未翻開且未配對的卡片可點擊
            if not card['matched'] and not card.get('flipped'):
                actions.append(MessageAction(
                    label=label,
                    text=f"翻牌:{card['id']}"
                ))
            else:
                # LINE不支持禁用按鈕，所以我們仍然添加按鈕但文本相同
                actions.append(MessageAction(
                    label=label,
                    text=f"翻牌:{card['id']}"
                ))
        
        # 確保至少有一個按鈕
        while len(actions) < 1:
            actions.append(MessageAction(
                label="無可用卡片",
                text="記憶翻牌遊戲"
            ))
        
        # 建立按鈕模板
        buttons_template = ButtonsTemplate(
            title=f"卡片 {i+1}-{i+len(card_group)}",
            text="請選擇要翻開的卡片",
            actions=actions[:4]  # 最多4個按鈕
        )
        
        card_messages.append(TemplateSendMessage(
            alt_text=f"卡片選擇 {i+1}-{i+len(card_group)}",
            template=buttons_template
        ))
    
    # 添加操作按鈕
    control_template = ButtonsTemplate(
        title="遊戲控制",
        text="選擇操作",
        actions=[
            MessageAction(label="重新開始", text="記憶翻牌遊戲"),
            MessageAction(label="返回主選單", text="返回主選單")
        ]
    )
    
    card_messages.append(TemplateSendMessage(
        alt_text="遊戲控制",
        template=control_template
    ))
    
    return [status_message] + card_messages
# === 學習功能模塊 ===
def show_category_menu():
    """顯示主題選單"""
    logger.info("顯示主題選單")
    
    quick_reply = QuickReply(
        items=[
            QuickReplyButton(action=MessageAction(label='日常用語', text='主題:日常用語')),
            QuickReplyButton(action=MessageAction(label='數字', text='主題:數字')),
            QuickReplyButton(action=MessageAction(label='動物', text='主題:動物')),
            QuickReplyButton(action=MessageAction(label='食物', text='主題:食物')),
            QuickReplyButton(action=MessageAction(label='交通工具', text='主題:交通工具'))
        ]
    )
    
    return TextSendMessage(
        text="請選擇您想學習的主題：",
        quick_reply=quick_reply
    )

def start_image_learning(user_id, category=None):
    """啟動圖像詞彙學習模式"""
    logger.info(f"啟動圖像詞彙學習模式，用戶ID: {user_id}")
    user_data = user_data_manager.get_user_data(user_id)
    user_data['current_activity'] = 'image_learning'
    
    # 如果指定了主題，設置當前主題
    if category:
        user_data['current_category'] = category
        word_key = random.choice(thai_data['categories'][category]['words'])
    # 隨機選擇詞彙，或從之前的主題中選擇
    elif user_data.get('current_vocab'):
        word_key = user_data['current_vocab']
    else:
        # 如果有當前主題，從該主題中選詞
        if user_data.get('current_category'):
            category = user_data['current_category']
            word_key = random.choice(thai_data['categories'][category]['words'])
        else:
            # 否則隨機選擇一個詞
            word_key = random.choice(list(thai_data['basic_words'].keys()))
    
    user_data['current_vocab'] = word_key
    word_data = thai_data['basic_words'][word_key]
    logger.info(f"選擇詞彙: {word_key}, 泰語: {word_data['thai']}")
    
    # 建立訊息列表
    message_list = []
    
    # 添加圖片
    if 'image_url' in word_data and word_data['image_url']:
        message_list.append(
            ImageSendMessage(
                original_content_url=word_data['image_url'],
                preview_image_url=word_data['image_url']
            )
        )
    
    # 添加詞彙訊息
    message_list.append(
        TextSendMessage(
            text=f"泰語：{word_data['thai']}\n中文：{word_key}\n發音：{word_data['pronunciation']}\n音調：{word_data['tone']}"
        )
    )
    
    # 添加選項按鈕
    buttons_template = ButtonsTemplate(
        title="詞彙學習",
        text="請選擇下一步",
        actions=[
            MessageAction(label="練習發音", text="練習發音"),
            MessageAction(label="下一個詞彙", text="下一個詞彙"),
            MessageAction(label="返回主選單", text="返回主選單")
        ]
    )
    message_list.append(
        TemplateSendMessage(alt_text="詞彙學習選項", template=buttons_template)
    )
    
    return message_list

def start_echo_practice(user_id):
    """啟動回音法發音練習"""
    logger.info(f"啟動回音法發音練習，用戶ID: {user_id}")
    user_data = user_data_manager.get_user_data(user_id)
    user_data['current_activity'] = 'echo_practice'

    # 獲取當前詞彙，若無則隨機選擇
    if not user_data.get('current_vocab'):
        # 如果有當前主題，從該主題中選詞
        if user_data.get('current_category'):
            category = user_data['current_category']
            word_key = random.choice(thai_data['categories'][category]['words'])
        else:
            # 否則隨機選擇一個詞
            word_key = random.choice(list(thai_data['basic_words'].keys()))
        user_data['current_vocab'] = word_key
    
    word_key = user_data['current_vocab']
    word_data = thai_data['basic_words'][word_key]
    logger.info(f"發音練習詞彙: {word_key}, 泰語: {word_data['thai']}")
    
    # 建立訊息列表
    message_list = []
    
    # 添加音訊提示
    if 'audio_url' in word_data and word_data['audio_url']:
        message_list.append(
            AudioSendMessage(
                original_content_url=word_data['audio_url'],
                duration=3000  # 假設音訊長度為3秒
            )
        )
    
    # 添加發音指導
    message_list.append(
        TextSendMessage(
            text=f"請聽標準發音，然後跟著練習：\n\n泰語：{word_data['thai']}\n發音：{word_data['pronunciation']}\n\n請點擊下方麥克風按鈕錄製您的發音"
        )
    )
    
    # 添加音調指導
    tone_info = ""
    for part in word_data['tone'].split('-'):
        if part in thai_data['tone_guide']:
            tone_info += thai_data['tone_guide'][part] + "\n"
    
    message_list.append(
        TextSendMessage(text=f"音調指南：\n{tone_info}")
    )
    
    # 添加錄音引導
    buttons_template = ButtonsTemplate(
        title="發音練習",
        text="請點擊錄音按鈕開始錄音",
        actions=[
            URIAction(
                label="開始錄音",
                uri="line://nv/camera/speech"
            ),
            MessageAction(label="返回主選單", text="返回主選單")
        ]
    )
    message_list.append(
        TemplateSendMessage(alt_text="發音練習", template=buttons_template)
    )
    
    return message_list

def start_tone_learning(user_id):
    """啟動音調學習模式"""
    logger.info(f"啟動音調學習模式，用戶ID: {user_id}")
    user_data = user_data_manager.get_user_data(user_id)
    user_data['current_activity'] = 'tone_learning'
    
    # 建立訊息列表
    message_list = []
    
    # 泰語音調介紹
    message_list.append(
        TextSendMessage(
            text="泰語有五種音調，不同音調會改變詞義：\n\n1. 中調 (無標記)\n2. 低調 (่)\n3. 降調 (้)\n4. 高調 (๊)\n5. 升調 (๋)"
        )
    )
    
    # 提供音調例子
    examples_text = "音調例子：\n\n"
    for example in thai_data['tone_examples']:
        examples_text += f"{example['thai']} - {example['meaning']} - {example['pronunciation']} ({example['tone']}調)\n"
    
    message_list.append(TextSendMessage(text=examples_text))
    
    # 添加選項按鈕
    buttons_template = ButtonsTemplate(
        title="音調學習",
        text="請選擇操作",
        actions=[
            MessageAction(label="練習發音", text="練習發音"),
            MessageAction(label="詞彙學習", text="詞彙學習"),
            MessageAction(label="返回主選單", text="返回主選單")
        ]
    )
    message_list.append(
        TemplateSendMessage(alt_text="音調學習選項", template=buttons_template)
    )
    
    return message_list

def start_dialogue_practice(user_id, dialogue_key=None):
    """啟動對話練習模式"""
    logger.info(f"啟動對話練習模式，用戶ID: {user_id}")
    user_data = user_data_manager.get_user_data(user_id)
    user_data['current_activity'] = 'dialogue_practice'
    
    # 如果沒有指定對話，隨機
    # 如果沒有指定對話，隨機選擇一個
    if not dialogue_key:
        dialogue_key = random.choice(list(thai_data['dialogues'].keys()))
    
    user_data['current_dialogue'] = dialogue_key
    dialogue = thai_data['dialogues'][dialogue_key]
    logger.info(f"選擇對話主題: {dialogue_key}")
    
    # 建立訊息列表
    message_list = []
    
    # 對話介紹
    message_list.append(
        TextSendMessage(text=f"對話主題：{dialogue_key}\n\n請學習以下對話：")
    )
    
    # 對話內容
    dialogue_text = ""
    for i, line in enumerate(dialogue):
        dialogue_text += f"{i+1}. {line['zh']} - {line['thai']} ({line['pronunciation']})\n"
    
    message_list.append(TextSendMessage(text=dialogue_text))
    
    # 添加選項按鈕
    buttons_template = ButtonsTemplate(
        title="對話練習",
        text="請選擇操作",
        actions=[
            MessageAction(label="練習對話", text="練習對話"),
            MessageAction(label="下一個對話", text="下一個對話"),
            MessageAction(label="返回主選單", text="返回主選單")
        ]
    )
    message_list.append(
        TemplateSendMessage(alt_text="對話練習選項", template=buttons_template)
    )
    
    return message_list

def show_learning_progress(user_id):
    """顯示用戶學習進度"""
    logger.info(f"顯示學習進度，用戶ID: {user_id}")
    user_data = user_data_manager.get_user_data(user_id)
    
    # 檢查是否有學習記錄
    if not user_data.get('vocab_mastery') or len(user_data['vocab_mastery']) == 0:
        return TextSendMessage(text="您還沒有開始學習。請選擇「詞彙學習」或「發音練習」開始您的泰語學習之旅！")
    
    # 統計學習數據
    total_words = len(user_data['vocab_mastery'])
    total_practices = sum(data['practice_count'] for data in user_data['vocab_mastery'].values())
    
    # 計算平均分數
    all_scores = []
    for data in user_data['vocab_mastery'].values():
        all_scores.extend(data['scores'])
    
    avg_score = sum(all_scores) / len(all_scores) if all_scores else 0
    
    # 找出最佳和需要改進的詞彙
    if total_words > 0:
        best_word = max(user_data['vocab_mastery'].items(), 
                         key=lambda x: sum(x[1]['scores'])/len(x[1]['scores']) if x[1]['scores'] else 0)
        worst_word = min(user_data['vocab_mastery'].items(), 
                          key=lambda x: sum(x[1]['scores'])/len(x[1]['scores']) if x[1]['scores'] else 100)
    
    # 格式化進度報告
    progress_report = f"學習進度報告：\n\n"
    progress_report += f"已學習詞彙：{total_words} 個\n"
    progress_report += f"練習次數：{total_practices} 次\n"
    progress_report += f"平均發音評分：{avg_score:.1f}/100\n"
    progress_report += f"遊戲總分：{user_data.get('score', 0)} 分\n"  # 添加遊戲分數
    progress_report += f"學習連續天數：{user_data['streak']} 天\n\n"
    
    if total_words > 0:
        progress_report += f"最佳發音詞彙：{best_word[0]} ({thai_data['basic_words'][best_word[0]]['thai']})\n"
        progress_report += f"需要加強的詞彙：{worst_word[0]} ({thai_data['basic_words'][worst_word[0]]['thai']})"
    
    # 添加進度按鈕
    buttons_template = ButtonsTemplate(
        title="學習進度",
        text="選擇下一步",
        actions=[
            MessageAction(label="練習弱點詞彙", text="練習弱點"),
            MessageAction(label="查看學習日曆", text="學習日曆"),
            MessageAction(label="返回主選單", text="返回主選單")
        ]
    )
    
    return [
        TextSendMessage(text=progress_report),
        TemplateSendMessage(alt_text="學習進度選項", template=buttons_template)
    ]

def show_main_menu():
    """顯示主選單"""
    logger.info("顯示主選單")
    
    # 使用 QuickReply 代替 ButtonsTemplate，因為 QuickReply 可以支援更多按鈕
    quick_reply = QuickReply(
        items=[
            QuickReplyButton(action=MessageAction(label='選擇主題', text='選擇主題')),
            QuickReplyButton(action=MessageAction(label='詞彙學習', text='詞彙學習')),
            QuickReplyButton(action=MessageAction(label='發音練習', text='練習發音')),
            QuickReplyButton(action=MessageAction(label='音調學習', text='音調學習')),
            QuickReplyButton(action=MessageAction(label='對話練習', text='對話練習')),
            QuickReplyButton(action=MessageAction(label='記憶翻牌遊戲', text='記憶翻牌遊戲')),
            QuickReplyButton(action=MessageAction(label='學習進度', text='學習進度'))
        ]
    )
    
    return TextSendMessage(
        text="🇹🇭 歡迎使用泰語學習系統 🇹🇭\n請選擇您想要的學習模式：",
        quick_reply=quick_reply
    )
# === LINE Bot Webhook 處理 ===
@app.route("/callback", methods=['POST'])
def callback():
    """處理LINE Webhook回調"""
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)
    
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        logger.error("無效的簽名")
        abort(400)
        
    return 'OK'

@handler.add(MessageEvent, message=TextMessage)
def handle_text_message(event):
    """處理文字訊息"""
    text = event.message.text
    user_id = event.source.user_id
    user_data = user_data_manager.get_user_data(user_id)
    
    logger.info(f"收到用戶 {user_id} 的文字訊息: {text}")
    
    # 更新用戶活躍狀態
    user_data_manager.update_streak(user_id)
    
    # 主選單與基本導航
    if text == "開始學習" or text == "返回主選單":
        line_bot_api.reply_message(event.reply_token, show_main_menu())
    
    # 選擇主題
    elif text == "選擇主題":
        line_bot_api.reply_message(event.reply_token, show_category_menu())
    
    # 主題選擇處理
    elif text.startswith("主題:"):
        category = text[3:]  # 取出主題名稱
        # 轉換成英文鍵值
        category_map = {
            "日常用語": "daily_phrases",
            "數字": "numbers",
            "動物": "animals",
            "食物": "food",
            "交通工具": "transportation"
        }
        if category in category_map:
            eng_category = category_map[category]
            user_data['current_category'] = eng_category
            messages = start_image_learning(user_id, eng_category)
            line_bot_api.reply_message(event.reply_token, messages)
        else:
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text="抱歉，無法識別該主題。請重新選擇。")
            )
    
    # 學習模式選擇
    elif text == "詞彙學習":
        messages = start_image_learning(user_id)
        line_bot_api.reply_message(event.reply_token, messages)
    
    elif text == "練習發音":
        messages = start_echo_practice(user_id)
        line_bot_api.reply_message(event.reply_token, messages)
    
    elif text == "音調學習":
        messages = start_tone_learning(user_id)
        line_bot_api.reply_message(event.reply_token, messages)
    
    elif text == "對話練習":
        messages = start_dialogue_practice(user_id)
        line_bot_api.reply_message(event.reply_token, messages)
    
    # 記憶翻牌遊戲
    elif text == "記憶翻牌遊戲":
        game_state = init_matching_game(user_id)
        messages = create_matching_game_message(game_state)
        line_bot_api.reply_message(event.reply_token, messages)
    
    elif text.startswith("翻牌:"):
        card_id = text[3:]  # 提取卡片ID
        game_state, result_message = handle_card_flip(user_id, card_id)
        
        if game_state:
            messages = [TextSendMessage(text=result_message)]
            # 如果遊戲結束，顯示總結訊息
            if game_state.get('matched_pairs') >= game_state.get('total_pairs'):
                game_complete_message = ButtonsTemplate(
                    title="遊戲完成！",
                    text=f"得分：{game_state.get('score', 0)}",
                    actions=[
                        MessageAction(label="再玩一次", text="記憶翻牌遊戲"),
                        MessageAction(label="返回主選單", text="返回主選單")
                    ]
                )
                messages.append(TemplateSendMessage(alt_text="遊戲完成", template=game_complete_message))
            else:
                # 更新遊戲界面
                messages.extend(create_matching_game_message(game_state))
            
            line_bot_api.reply_message(event.reply_token, messages)
        else:
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text=result_message)
            )
    
    # 進度與導航控制
    elif text == "下一個詞彙":
        # 如果有當前主題，在同一主題中選擇新詞彙
        if user_data.get('current_category'):
            category = user_data['current_category']
            user_data['current_vocab'] = random.choice(thai_data['categories'][category]['words'])
        else:
            # 否則清除當前詞彙，隨機選擇
            user_data['current_vocab'] = None
        
        messages = start_image_learning(user_id)
        line_bot_api.reply_message(event.reply_token, messages)
    
    elif text == "下一個對話":
        # 清除當前對話，開始新一輪對話練習
        messages = start_dialogue_practice(user_id)
        line_bot_api.reply_message(event.reply_token, messages)
    
    elif text == "學習進度":
        progress_message = show_learning_progress(user_id)
        line_bot_api.reply_message(event.reply_token, progress_message)
    
    elif text == "練習弱點":
        # 找出評分最低的詞彙進行練習
        if not user_data.get('vocab_mastery') or len(user_data['vocab_mastery']) == 0:
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text="您還沒有足夠的學習記錄，請先進行一些詞彙學習和發音練習！")
            )
            return
            
        # 找出分數最低的詞彙
        worst_word = min(user_data['vocab_mastery'].items(), 
                      key=lambda x: sum(x[1]['scores'])/len(x[1]['scores']) if x[1]['scores'] else 100)
        
        # 設置為當前詞彙並啟動練習
        user_data['current_vocab'] = worst_word[0]
        messages = start_echo_practice(user_id)
        line_bot_api.reply_message(event.reply_token, messages)
    
    elif text == "學習日曆":
        # 顯示用戶的學習日曆和連續學習天數
        streak = user_data.get('streak', 0)
        last_active = user_data.get('last_active', '尚未開始學習')
        
        calendar_message = f"您的學習記錄：\n\n"
        calendar_message += f"連續學習天數：{streak} 天\n"
        calendar_message += f"最近學習日期：{last_active}\n\n"
        calendar_message += "繼續保持學習熱情！每天學習一點，泰語能力會穩步提高。"
        
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text=calendar_message)
        )
    
    elif text == "練習對話":
        # 如果用戶已經有當前對話，發送練習提示
        if user_data.get('current_dialogue'):
            dialogue_key = user_data['current_dialogue']
            dialogue = thai_data['dialogues'][dialogue_key]
            
            practice_tips = f"請嘗試記住並練習以下對話：\n\n"
            for i, line in enumerate(dialogue):
                practice_tips += f"{i+1}. {line['zh']} - {line['thai']}\n"
            
            practice_tips += "\n提示：先遮住泰語部分，嘗試翻譯中文句子，然後對照檢查。"
            
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text=practice_tips)
            )
        else:
            # 否則開始新的對話練習
            messages = start_dialogue_practice(user_id)
            line_bot_api.reply_message(event.reply_token, messages)
    
    else:
        # 默認回覆
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text="請選擇「開始學習」或點擊選單按鈕開始泰語學習之旅")
        )
        @handler.add(MessageEvent, message=AudioMessage)
def handle_audio_message(event):
    """處理音頻消息，主要用於發音評估"""
    user_id = event.source.user_id
    user_data = user_data_manager.get_user_data(user_id)
    
    logger.info(f"收到用戶 {user_id} 的音頻訊息")
    
    # 檢查用戶是否在發音練習中
    if user_data.get('current_activity') == 'echo_practice':
        try:
            # 獲取音訊內容
            audio_content = get_audio_content(event.message.id)
            
            # 獲取當前詞彙
            word_key = user_data.get('current_vocab')
            if not word_key:
                line_bot_api.reply_message(
                    event.reply_token,
                    TextSendMessage(text="請先選擇一個詞彙進行學習")
                )
                return
                
            word_data = thai_data['basic_words'][word_key]
            
            # 處理音頻並轉換格式
            audio_file_path = process_audio_content(audio_content)
            
            # 使用Azure評估發音
            assessment_result = evaluate_pronunciation(
                audio_file_path, 
                word_data['thai'],
                language="th-TH"
            )
            
            # 準備回應訊息
            if assessment_result["success"]:
                score = assessment_result["overall_score"]
                
                # 根據分數生成反饋
                if score >= 90:
                    feedback = f"太棒了！您的 '{word_key}' ({word_data['thai']}) 發音非常準確。"
                elif score >= 75:
                    feedback = f"做得好！您的 '{word_key}' ({word_data['thai']}) 發音清晰，繼續保持。"
                elif score >= 60:
                    feedback = f"不錯的嘗試。您的 '{word_key}' ({word_data['thai']}) 發音基本正確，但可以再加強音調。"
                else:
                    feedback = f"繼續練習！注意 '{word_key}' ({word_data['thai']}) 的音調變化，可多聽幾次標準發音。"
                
                # 更新用戶學習進度
                if 'vocab_mastery' not in user_data:
                    user_data['vocab_mastery'] = {}
                
                if word_key not in user_data['vocab_mastery']:
                    user_data['vocab_mastery'][word_key] = {
                        'practice_count': 1,
                        'scores': [score],
                        'last_practiced': datetime.now().strftime("%Y-%m-%d")
                    }
                else:
                    user_data['vocab_mastery'][word_key]['practice_count'] += 1
                    user_data['vocab_mastery'][word_key]['scores'].append(score)
                    user_data['vocab_mastery'][word_key]['last_practiced'] = datetime.now().strftime("%Y-%m-%d")
                
                logger.info(f"用戶 {user_id} 的 '{word_key}' 發音評分: {score}")
                
                # 詳細評分內容
                details = f"發音評估詳情：\n" \
                         f"整體評分：{score}/100\n" \
                         f"準確度：{assessment_result['accuracy_score']}/100\n" \
                         f"發音清晰度：{assessment_result['pronunciation_score']}/100\n" \
                         f"完整度：{assessment_result['completeness_score']}/100\n" \
                         f"流暢度：{assessment_result['fluency_score']}/100"
                
                # 建立回覆訊息
                messages = [
                    TextSendMessage(text=f"發音評分：{score}/100"),
                    TextSendMessage(text=feedback),
                    TextSendMessage(text=details)
                ]
                
                # 添加選項按鈕
                buttons_template = ButtonsTemplate(
                    title="發音評估結果",
                    text="請選擇下一步",
                    actions=[
                        MessageAction(label="再次練習", text="練習發音"),
                        MessageAction(label="下一個詞彙", text="下一個詞彙"),
                        MessageAction(label="返回主選單", text="返回主選單")
                    ]
                )
                messages.append(
                    TemplateSendMessage(alt_text="發音評估選項", template=buttons_template)
                )
                
                line_bot_api.reply_message(event.reply_token, messages)
            else:
                # 發生錯誤
                error_msg = assessment_result.get("error", "未知錯誤")
                logger.error(f"發音評估失敗: {error_msg}")
                line_bot_api.reply_message(
                    event.reply_token,
                    [
                        TextSendMessage(text=f"發音評估失敗：{error_msg}"),
                        TextSendMessage(text="請重新嘗試發音，或選擇其他詞彙學習")
                    ]
                )
        except Exception as e:
            # 處理例外
            logger.error(f"處理音頻時發生錯誤: {str(e)}")
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text=f"處理您的錄音時發生錯誤：{str(e)}\n請重新嘗試或聯繫系統管理員。")
            )
    else:
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text="請先選擇「練習發音」開始發音練習")
        )

# 測試 Azure 語音服務連接
def test_azure_connection():
    """測試 Azure 語音服務連接"""
    try:
        speech_config = speechsdk.SpeechConfig(subscription=speech_key, region=speech_region)
        logger.info("Azure Speech Services 連接測試成功")
    except Exception as e:
        logger.error(f"Azure Speech Services 連接測試失敗: {str(e)}")

# 直接在模組層級調用這個函數
test_azure_connection()

if __name__ == "__main__":
    # 啟動 Flask 應用，使用環境變數設定的端口或默認5000
    port = int(os.environ.get('PORT', 5000))
    logger.info(f"應用啟動在端口 {port}")
    app.run(host='0.0.0.0', port=port)