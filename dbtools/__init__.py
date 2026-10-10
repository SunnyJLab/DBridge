"""MCP DB tools: SELECT-only Oracle access behind a typed tool registry.

이 패키지는 재export 파사드를 두지 않는다. 필요한 것은 하위 모듈에서 직접
가져와라 (``from dbtools.service import ToolService``, ``from dbtools.errors
import ToolError`` 등). 예전에는 여기서 30여 개 이름을 재export했는데,
그 때문에 ``import dbtools.errors`` 같은 리프 모듈 하나를 가져와도
``service`` → ``tools`` → 전 모듈이 적재되어 트랜스포트가 함수 내부
import로 이 사슬을 우회해야 했다.

계층: ``errors`` → {``sql_safety``, ``database``, ``registry``} →
{``context``, ``catalog``, ``extensions``} → ``common`` → ``tools`` → ``service``.
패키지를 import 하는 것만으로는 어떤 부작용도 없다 (로깅 설정·드라이버 적재·
레지스트리 채우기 모두 없음).
"""
