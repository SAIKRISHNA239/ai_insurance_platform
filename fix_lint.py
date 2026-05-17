
import re

def replace_in_file(filepath, replacements):
    try:
        with open(filepath, 'r') as f:
            content = f.read()
            
        original_content = content
        for old, new in replacements:
            content = content.replace(old, new)
            
        if content != original_content:
            with open(filepath, 'w') as f:
                f.write(content)
            print(f"Updated {filepath}")
    except Exception as e:
        print(f"Error updating {filepath}: {e}")

def regex_replace_in_file(filepath, replacements):
    try:
        with open(filepath, 'r') as f:
            content = f.read()
            
        original_content = content
        for pattern, new in replacements:
            content = re.sub(pattern, new, content)
            
        if content != original_content:
            with open(filepath, 'w') as f:
                f.write(content)
            print(f"Updated {filepath}")
    except Exception as e:
        print(f"Error updating {filepath}: {e}")

# 8. backend/claims/snip_validator.py
regex_replace_in_file('backend/claims/snip_validator.py', [
    (r'import uuid\n', ''),
    (r'from backend.claims.schemas import EDIClaimPayload, EDIProcedureLine', 'from backend.claims.schemas import EDIClaimPayload'),
])

# 9. backend/claims/state_machine.py
regex_replace_in_file('backend/claims/state_machine.py', [
    (r'from typing import Any, Callable, TypeVar', 'from typing import Any, TypeVar'),
    (r'datetime\.utcnow\(\)', 'datetime.now(timezone.utc)'),
    (r'from datetime import datetime\n', 'from datetime import datetime, timezone\n'),
])

# 10. backend/config.py
regex_replace_in_file('backend/config.py', [
    (r'from pydantic import AnyUrl, PostgresDsn, field_validator\n', ''),
])

# 11. backend/database/base.py
regex_replace_in_file('backend/database/base.py', [
    (r'from typing import Any, AsyncGenerator\n', 'from typing import AsyncGenerator\n'),
    (r'from sqlalchemy\.orm import DeclarativeBase, MappedColumn, declared_attr', 'from sqlalchemy.orm import DeclarativeBase, declared_attr'),
])

# 12. backend/embeddings/service.py
regex_replace_in_file('backend/embeddings/service.py', [
    (r'import hashlib\n', ''),
    (r'from functools import lru_cache\n', ''),
])

# 13. backend/main.py
regex_replace_in_file('backend/main.py', [
    (r'from backend\.database\.models import Base\n', ''),
])

# 14. backend/middleware/auth.py
regex_replace_in_file('backend/middleware/auth.py', [
    (r'from typing import Any, Callable, Optional', 'from typing import Any, Optional'),
])

# 15. backend/underwriting/ai_assistant.py
regex_replace_in_file('backend/underwriting/ai_assistant.py', [
    (r'from dataclasses import dataclass\n', ''),
    (r'f"Underwriting Summary Report"', '"Underwriting Summary Report"'),
    (r'f"Risk Factors Identified"', '"Risk Factors Identified"'),
    (r'f"Mitigating Factors Identified"', '"Mitigating Factors Identified"'),
    (r'f"Missing Information Requirements"', '"Missing Information Requirements"'),
])

# 16. backend/underwriting/external_apis.py
regex_replace_in_file('backend/underwriting/external_apis.py', [
    (r'from typing import Any\n', ''),
])

# 17. backend/underwriting/scoring.py
regex_replace_in_file('backend/underwriting/scoring.py', [
    (r'from decimal import Decimal\n', ''),
])

# 18. backend/workflows/claims_workflow.py
regex_replace_in_file('backend/workflows/claims_workflow.py', [
    (r'from typing import Any, AsyncGenerator', 'from typing import AsyncGenerator'),
])

# 19. backend/workflows/events.py
regex_replace_in_file('backend/workflows/events.py', [
    (r'import asyncio\n', ''),
    (r'import time\n', ''),
    (r'datetime\.utcnow\(\)', 'datetime.now(timezone.utc)'),
    (r'from datetime import datetime\n', 'from datetime import datetime, timezone\n'),
    (r'for l in ', 'for line in '),
    (r'\[l for l in ', '[line for line in '),
    (r' l\.', ' line.'),
    (r'\(l\)', '(line)'),
    (r'\(l\.', '(line.'),
])

# 20. backend/workflows/saga_coordinator.py
regex_replace_in_file('backend/workflows/saga_coordinator.py', [
    (r'import asyncio\n', ''),
    (r'datetime\.utcnow\(\)', 'datetime.now(timezone.utc)'),
    (r'from datetime import datetime\n', 'from datetime import datetime, timezone\n'),
])

# 21. load_testing/locustfile.py
regex_replace_in_file('load_testing/locustfile.py', [
    (r'from decimal import Decimal\n', ''),
    (r'f"99213"', '"99213"'),
])
