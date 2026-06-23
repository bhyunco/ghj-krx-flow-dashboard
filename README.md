# GHJ KRX Flow Dashboard

KRX 정보데이터시스템 수급 데이터를 Selenium 없이 API 방식으로 조회하고, 웹 화면에서 기본 분석/시각화/엑셀 다운로드를 제공하는 Flask 기반 로컬 앱입니다.

## 실행

```powershell
python ghj_codex_V_03.py
```

브라우저가 자동으로 열립니다. 열리지 않으면 아래 주소로 접속합니다.

```text
http://127.0.0.1:5000
```

## 입력값

- KRX 아이디
- KRX 비밀번호
- KRX OpenAPI 키
- 기준일
- 시장 구분

KRX OpenAPI 키 발급 주소:

```text
https://openapi.krx.co.kr/
```

## 자동 분석 범위

프로그램은 사용자가 기간을 직접 선택하지 않도록 아래 범위를 고정 실행합니다.

- 6개월 누적
- 3개월 누적
- 1개월 누적
- 최근 거래일 5개 일별 데이터

주말과 휴장일은 자동으로 건너뜁니다.

## 결과

조회 완료 후 웹 화면에 기본 차트와 주요 종목 미리보기 테이블이 표시됩니다.

결과 엑셀은 아래 경로에 저장됩니다.

```text
outputs/YYYYMMDD/YYYY-MM-DD_통합파일.xlsx
```

웹 화면의 `엑셀 다운로드` 버튼으로도 받을 수 있습니다.

## 문서

- `프로그램_상세설명.md`
- `처음사용자_준비방법.md`

실행 후 웹에서 아래 주소로 상세 설명을 볼 수 있습니다.

```text
http://127.0.0.1:5000/docs
```

## 배포 예정 구조

다음 단계에서 아래 구조로 배포 리팩터링을 진행할 예정입니다.

- GitHub: 코드 저장소
- Supabase: 작업 상태/결과 메타데이터/파일 저장
- Vercel: 웹 앱 배포

## Vercel 설정

Vercel Python 런타임이 Flask 앱을 찾을 수 있도록 `pyproject.toml`에 아래 entrypoint를 지정했습니다.

```toml
[tool.vercel]
entrypoint = "ghj_codex_V_03:app"
```
