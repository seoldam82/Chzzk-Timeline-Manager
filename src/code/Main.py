import os
import sys
import math
import signal
import re
import glob

current_dir = os.path.dirname(os.path.abspath(__file__))
if current_dir not in sys.path:
    sys.path.append(current_dir)

from Chzzk_api import (
    select_chzzk_vod,
    download_chzzk_vod_chats,
    CONFIG
)
from Timeline import (
    download_chzzk_vod_audio,
    transcribe_chzzk_audio,
    generate_chzzk_timeline,
    merge_and_format_final_timeline,
    timestamp_to_seconds,
    correct_streamer_nicknames_with_codex,
    ensure_codex_ready,
    load_chzzk_streamers_raw_db,
    prepare_streamer_profile,
)

def parse_chat_timestamp_to_secs(chat_line):
    match = re.match(r"^\[(\d{2}):(\d{2}):(\d{2})\]", chat_line)
    if match:
        h, m, s = map(int, match.groups())
        return h * 3600 + m * 60 + s

    match_short = re.match(r"^\[(\d{2}):(\d{2})\]", chat_line)
    if match_short:
        m, s = map(int, match_short.groups())
        return m * 60 + s

    return None


def parse_time_input(value):
    """Parse MM:SS or HH:MM:SS user input into absolute VOD seconds."""
    parts = value.strip().split(":")
    if len(parts) not in (2, 3) or any(not part.isdigit() for part in parts):
        raise ValueError("시간은 MM:SS 또는 HH:MM:SS 형식이어야 합니다.")

    numbers = [int(part) for part in parts]
    if len(numbers) == 2:
        hours = 0
        minutes, seconds = numbers
    else:
        hours, minutes, seconds = numbers

    if minutes >= 60 or seconds >= 60:
        raise ValueError("분과 초는 0부터 59 사이여야 합니다.")
    return hours * 3600 + minutes * 60 + seconds


def format_time_label(total_seconds):
    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    seconds = total_seconds % 60
    return f"{hours:02d}-{minutes:02d}-{seconds:02d}"


def ask_analysis_time_range(total_duration_secs):
    duration_text = format_time_label(total_duration_secs).replace("-", ":")
    range_text = f"00:00:00 ~ {duration_text}"
    while True:
        start_input = input(
            f"➡️ 분석 시작 시간을 입력하세요 (영상 범위: {range_text}, "
            "MM:SS 또는 HH:MM:SS, 기본값: 00:00): "
        ).strip()
        end_input = input(
            f"➡️ 분석 종료 시간을 입력하세요 (영상 범위: {range_text}, "
            f"MM:SS 또는 HH:MM:SS, 기본값: {duration_text}): "
        ).strip()
        try:
            start_sec = parse_time_input(start_input or "00:00")
            end_sec = parse_time_input(end_input or duration_text)
            if start_sec >= end_sec:
                raise ValueError("종료 시간은 시작 시간보다 뒤여야 합니다.")
            if end_sec > total_duration_secs:
                raise ValueError(f"종료 시간이 VOD 길이({duration_text})를 넘을 수 없습니다.")
            return start_sec, end_sec
        except ValueError as error:
            print(f"❌ {error} 다시 입력해 주세요.")


def show_collab_member_reference_preview(preview_lines=12):
    filename = "chzzk_streamers.txt"
    db_path = os.path.abspath(filename)
    raw_content = load_chzzk_streamers_raw_db(filename)

    print(f"\n📄 [{filename}] 내용 미리보기 (최대 {preview_lines}줄)")
    print("-------------------------------------------------------------------------")
    if not raw_content:
        print("(파일 내용이 없거나 읽을 수 없습니다.)")
    else:
        lines = raw_content.splitlines()
        for line in lines[:preview_lines]:
            print(line)
        if len(lines) > preview_lines:
            print(f"... (이하 {len(lines) - preview_lines}줄 생략)")
    print("-------------------------------------------------------------------------")
    print(f"💡 전체 파일 열기: {db_path}")


def ask_use_collab_member_reference():
    show_collab_member_reference_preview()

    while True:
        answer = input(
            "➡️ 합방 멤버 참고 목록을 사용할까요? "
            "(y/n/e, 기본값: y / e: 파일 열기): "
        ).strip().lower()

        if answer in {"e", "edit", "편집"}:
            try:
                os.startfile(os.path.abspath("chzzk_streamers.txt"))
                print("✏️ chzzk_streamers.txt를 기본 편집기로 열었습니다.")
                print("   수정 후 이 창으로 돌아와 y 또는 n을 입력하세요.")
            except Exception as e:
                print(f"⚠️ 파일을 자동으로 열지 못했습니다: {e}")
                print(f"   직접 열어 수정해 주세요: {os.path.abspath('chzzk_streamers.txt')}")
            continue

        if answer in {"", "y", "yes", "예", "ㅇ"}:
            return True

        if answer in {"n", "no", "아니오", "ㄴ"}:
            return False

        print("❌ y(사용), n(미사용), e(파일 열기) 중 하나를 입력해 주세요.")


def process_direct_comment_mode():
    print("\n-------------------------------------------------------------------------")
    print("📂 기존 타임라인 초안 열기 및 수정 모드")
    print("-------------------------------------------------------------------------")

    txt_files = glob.glob("TL_VOD_*.txt")

    if not txt_files:
        print("❌ 현재 디렉토리에 'TL_VOD_...' 형식으로 생성된 텍스트 파일이 없습니다.")
        print("💡 모드 1을 선택해 타임라인을 먼저 새로 생성해 주세요.")
        return

    print("📝 수정할 로컬 타임라인 초안 목록:")
    for idx, filepath in enumerate(txt_files, 1):
        print(f" [{idx}] {filepath}")

    try:
        choice = int(input("\n👉 열 파일 번호를 선택하세요: ").strip())
        if choice < 1 or choice > len(txt_files):
            print("❌ 잘못된 번호입니다. 프로그램을 종료합니다.")
            return
        selected_file = txt_files[choice - 1]
    except ValueError:
        print("❌ 숫자로 입력해 주세요. 프로그램을 종료합니다.")
        return

    try:
        os.startfile(os.path.abspath(selected_file))
        print(f"✏️ '{selected_file}' 초안을 기본 텍스트 편집기로 열었습니다.")
        print("수정한 뒤 내용을 복사해 치지직 VOD 댓글 입력창에 직접 붙여넣고 게시해 주세요.")
    except Exception as e:
        print(f"⚠️ 편집기를 자동으로 열지 못했습니다: {e}")
        print(f"초안 파일을 직접 열어 수정해 주세요: {os.path.abspath(selected_file)}")


def load_prepared_timeline_materials(vod_id):
    full_script_path = os.path.join(
        os.getcwd(), "voicepalette", f"VOD_{vod_id}", "full_raw_script.txt"
    )
    full_chat_path = os.path.join(
        os.getcwd(), "chat_cache", str(vod_id), f"chat_{vod_id}_full.txt"
    )

    missing_materials = []
    if not os.path.isfile(full_script_path) or os.path.getsize(full_script_path) <= 10:
        missing_materials.append(f"STT 대본: {full_script_path}")
    if not os.path.isfile(full_chat_path) or os.path.getsize(full_chat_path) <= 0:
        missing_materials.append(f"채팅 캐시: {full_chat_path}")

    if missing_materials:
        print("❌ 타임라인만 다시 만들기 위한 재료가 부족합니다.")
        for material in missing_materials:
            print(f"   - {material}")
        print("💡 먼저 일반 생성 모드를 한 번 실행해 STT 대본과 채팅 캐시를 준비해 주세요.")
        return ""

    try:
        with open(full_script_path, "r", encoding="utf-8") as f:
            full_transcription = f.read()
    except (OSError, UnicodeError) as e:
        print(f"❌ 준비된 STT 대본을 읽지 못했습니다: {e}")
        return ""

    if not full_transcription.strip():
        print("❌ 준비된 STT 대본이 비어 있습니다.")
        return ""

    print("✨ [재료 재사용] 오디오 다운로드와 STT 변환을 건너뜁니다.")
    print(f"   - STT 대본: {full_script_path}")
    print(f"   - 채팅 캐시: {full_chat_path}")
    return full_transcription


def run_pure_test(timeline_only=False):
    print("\n-------------------------------------------------------------------------")
    print("🤖 AI 기반 새 VOD 타임라인 생성 및 추출 모드 시작")
    print("-------------------------------------------------------------------------")

    TARGET_CHANNEL_ID = CONFIG.get("TARGET_CHANNEL_ID")
    CODEX_MODEL = CONFIG.get("CODEX_MODEL", "")
    WHISPER_MODEL = CONFIG.get("WHISPER_MODEL", "base")

    try:
        ensure_codex_ready()
    except Exception as e:
        print(f"❌ Codex 준비 상태 확인 실패: {e}")
        return

    voicepalette_BASE_DIR = "./voicepalette"
    if not os.path.exists(voicepalette_BASE_DIR):
        os.makedirs(voicepalette_BASE_DIR)

    try:
        vod_limit_input = input("➡️ 불러올 최근 VOD 개수를 입력하세요 (기본값: 10): ").strip()
        vod_limit = int(vod_limit_input) if vod_limit_input else 10
        if vod_limit <= 0:
            vod_limit = 10
    except ValueError:
        print("❌ 올바른 숫자가 아닙니다. 기본값인 10개로 탐색을 시작합니다.")
        vod_limit = 10

    selected_vod = select_chzzk_vod(
        TARGET_CHANNEL_ID,
        limit=vod_limit,
    )
    if selected_vod and len(selected_vod) == 5:
        vod_id, actual_title, video_duration, target_streamer, target_channel_id = selected_vod
    elif selected_vod and len(selected_vod) == 4:
        vod_id, actual_title, video_duration, target_streamer = selected_vod
        target_channel_id = None
    else:
        print("❌ 유효한 치지직 VOD 일련번호를 획득하지 못했습니다.")
        return
    if not vod_id:
        print("❌ 유효한 치지직 VOD 일련번호를 획득하지 못했습니다.")
        return
    if not target_streamer:
        print("⚠️ 선택한 VOD에서 채널명을 확인하지 못해 주인공 스트리머명을 비워 둡니다.")

    target_streamer, streamer_profile_context = prepare_streamer_profile(
        target_channel_id=target_channel_id,
        target_streamer=target_streamer,
    )

    full_vod_url = f"https://chzzk.naver.com/video/{vod_id}"
    print(f"\n🎬 선택된 타겟 방송: [{actual_title}] (VOD ID: {vod_id})")

    if not video_duration or video_duration <= 0:
        print("❌ VOD 길이를 확인하지 못해 시간 구간을 안전하게 계산할 수 없습니다.")
        return
    total_duration_secs = int(video_duration)
    global_start_sec, global_end_sec = ask_analysis_time_range(total_duration_secs)

    use_collab_member_reference = ask_use_collab_member_reference()
    if use_collab_member_reference:
        print("👥 합방 멤버 자동 감지 참고 목록을 사용합니다.")
    else:
        print("👥 합방 멤버 자동 감지 참고 목록을 사용하지 않습니다.")

    print(f"⏱️ 영상 총 환산 시간: 약 {total_duration_secs}초")
    print(f"🎯 전체 분석 타겟 구간: {global_start_sec}초 ~ {global_end_sec}초 범위")

    if timeline_only:
        full_transcription = load_prepared_timeline_materials(vod_id)
    else:
        is_full_range = global_start_sec == 0 and global_end_sec == total_duration_secs
        master_audio_path = download_chzzk_vod_audio(
            chzzk_url=full_vod_url,
            vod_id=vod_id,
            start_sec=global_start_sec,
            end_sec=None if is_full_range else global_end_sec,
        )
        if not master_audio_path or not os.path.exists(master_audio_path):
            print("❌ 전체 오디오 캐시 데이터 생성 과정에 실패했습니다.")
            return

        script_filename = (
            "full_raw_script.txt" if is_full_range
            else f"raw_script_{global_start_sec}_{global_end_sec}.txt"
        )
        full_script_path = os.path.join(
            os.getcwd(), "voicepalette", f"VOD_{vod_id}", script_filename
        )
        full_transcription = transcribe_chzzk_audio(
            audio_path=master_audio_path,
            target_path=full_script_path,
            model_size=WHISPER_MODEL,
            timestamp_offset_sec=global_start_sec,
        )
    if not full_transcription.strip():
        print("❌ VOD 전체 대본(STT) 데이터가 유효하지 않거나 비어있습니다.")
        return

    script_lines = full_transcription.split("\n")
    CHUNK_SIZE_SECS = 3600
    all_raw_items = []

    current_chunk_start = global_start_sec
    while current_chunk_start < global_end_sec:
        current_chunk_end = min(current_chunk_start + CHUNK_SIZE_SECS, global_end_sec)
        chunk_index = int(current_chunk_start // CHUNK_SIZE_SECS)

        print(f"\n[🔄 청크 슬라이싱] {current_chunk_start}초 ~ {current_chunk_end}초 구간 텍스트 추출 중... (인덱스: {chunk_index})")

        compressed_chat_data = download_chzzk_vod_chats(vod_id, current_chunk_start, current_chunk_end)
        chunk_script_lines = []
        for line in script_lines:
            line_strip = line.strip()
            if not line_strip:
                continue
            match = re.match(r"^\[(\d+:\d+:\d+)\]", line_strip)
            if match:
                line_secs = timestamp_to_seconds(match.group(1))
                if current_chunk_start <= line_secs < current_chunk_end:
                    chunk_script_lines.append(line_strip)

        chunk_transcription_text = "\n".join(chunk_script_lines)

        if not chunk_transcription_text.strip():
            print(f"⚠️ [{chunk_index}번 청크] 해당 시간 구간에 매칭되는 STT 텍스트 대본이 없습니다. 건너뜁니다.")
            current_chunk_start = current_chunk_end
            continue

        try:
            with open(os.path.join(os.getcwd(), "voicepalette", "last_raw_script.txt"), "w", encoding="utf-8") as lf:
                lf.write(chunk_transcription_text)
        except:
            pass

        print(f"🚀 Codex 구독 모델 호출 중 (청크 인덱스: {chunk_index})...")
        chunk_items = generate_chzzk_timeline(
            input_script=chunk_transcription_text,
            chat_script=compressed_chat_data,
            actual_title=actual_title,
            chzzk_url=full_vod_url,
            codex_model=CODEX_MODEL,
            chunk_index=chunk_index,
            use_collab_member_reference=use_collab_member_reference,
            target_streamer=target_streamer,
            target_channel_id=target_channel_id,
            streamer_profile_context=streamer_profile_context,
        )

        if chunk_items:
            all_raw_items.extend(chunk_items)

        current_chunk_start = current_chunk_end

    if not all_raw_items:
        print("❌ Codex가 정상적인 타임라인 항목 뼈대를 생성하지 못했습니다.")
        return

    # 1. 1차 취합 데이터 문자열 빌드
    final_output_text = merge_and_format_final_timeline(all_raw_items)

    # 🚨 [최종단계 Codex 보정] 문맥 분석 권한을 위임받은 함수 호출 수행
    print("⚙️  [최종 후처리] Codex 모델을 활용한 문맥/유사도 기반 닉네임 교정 작업 수행 중...")
    final_output_text = correct_streamer_nicknames_with_codex(
        timeline_text=final_output_text,
        codex_model=CODEX_MODEL,
        db_filename="chzzk_streamers.txt"
    )

    ai_notice = "🤖 이 댓글은 방송 하이라이트를 AI가 분석하여 생성한 타임라인으로 다소 부정확한 부분이 있을 수 있습니다."
    new_header = f"[00:00:00] {actual_title}"

    lines = final_output_text.split("\n")
    cleaned_final_lines = []

    for line in lines:
        line_strip = line.strip().replace("🔥", "")
        line_strip = re.sub(r'\[\[(\d{2}:\d{2}:\d{2})\]\]', r'[\1]', line_strip)
        line_strip = re.sub(r'\[\[(\d{2}:\d{2})\]\]', r'[\1]', line_strip)

        if "🤖 이 댓글은" in line_strip or line_strip.startswith("[00:00:00]"):
            continue
        cleaned_final_lines.append(line_strip)

    cleaned_final_lines.insert(0, "")
    cleaned_final_lines.insert(0, new_header)
    cleaned_final_lines.insert(0, ai_notice)

    final_timeline_string = "\n".join(cleaned_final_lines)
    start_label = format_time_label(global_start_sec)
    end_label = format_time_label(global_end_sec)
    output_path = f"TL_VOD_{vod_id}_{start_label}_{end_label}.txt"

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(final_timeline_string)

    try:
        os.startfile(os.path.abspath(output_path))
        print(f"✏️ 초안을 기본 텍스트 편집기로 열었습니다: '{output_path}'")
    except Exception as e:
        print(f"⚠️ 편집기를 자동으로 열지 못했습니다: {e}")
        print(f"초안 파일을 직접 열어 수정해 주세요: {os.path.abspath(output_path)}")

    print("\n=========================================================================")
    print("🎯 [완성] 모든 청크 취합 및 가공이 완료된 최종 타임라인 결과")
    print("=========================================================================")
    print(final_timeline_string)
    print("=========================================================================")
    print(f"💾 최종 타임라인 결과 파일이 '{output_path}'로 안전하게 출력되었습니다!")

    timeline_len = len(final_timeline_string)
    print(f"\n📊 현재 생성된 타임라인 글자 수: {timeline_len}자 / 5000자")

    if timeline_len > 5000:
        print("⚠️ [경고] 타임라인 총 길이가 5000자를 초과하여 치지직 댓글 시스템에 등록할 수 없습니다.")
        print("💡 해결책: 분석할 VOD 범위를 조금 더 좁게 나누어 청크 처리를 시도하세요.")
    else:
        print(f"✏️ 초안을 수정한 뒤 내용을 복사해 VOD [{vod_id}] 댓글창에 붙여넣고 직접 게시해 주세요.")


if __name__ == "__main__":
    signal.signal(signal.SIGINT, lambda sig, frame: sys.exit(0))

    print("=========================================================================")
    print("                  치지직 VOD 타임라인 매니저                 ")
    print("=========================================================================")
    print(" [1] 새 재료를 준비하고 타임라인 파일 생성하기")
    print(" [2] 준비된 재료로 타임라인만 다시 만들기")
    print(" [3] 기존 타임라인 초안을 열어 수정하기")
    print("-------------------------------------------------------------------------")

    menu = input("👉 원하시는 모드 번호를 선택하세요 (1, 2 또는 3): ").strip()

    if menu == "1":
        run_pure_test()
    elif menu == "2":
        run_pure_test(timeline_only=True)
    elif menu == "3":
        process_direct_comment_mode()
    else:
        print("❌ 올바른 선택이 아닙니다. 프로그램을 종료합니다.")
