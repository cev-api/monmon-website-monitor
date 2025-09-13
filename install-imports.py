# This script installs the required imports - easy!

try:
    __import__('bs4')
except ImportError:
    import pip
    pip.main(['install', 'bs4'])
try:
    __import__('rich.prompt')
except ImportError:
    import pip
    pip.main(['install', 'rich.prompt'])
try:
    __import__('os')
except ImportError:
    import pip
    pip.main(['install', 'os'])
try:
    __import__('rich.panel')
except ImportError:
    import pip
    pip.main(['install', 'rich.panel'])
try:
    __import__('rich.console')
except ImportError:
    import pip
    pip.main(['install', 'rich.console'])
try:
    __import__('sys')
except ImportError:
    import pip
    pip.main(['install', 'sys'])
try:
    __import__('dataclasses')
except ImportError:
    import pip
    pip.main(['install', 'dataclasses'])
try:
    __import__('rich.text')
except ImportError:
    import pip
    pip.main(['install', 'rich.text'])
try:
    __import__('playwright.sync_api')
except ImportError:
    import pip
    pip.main(['install', 'playwright.sync_api'])
try:
    __import__('threading')
except ImportError:
    import pip
    pip.main(['install', 'threading'])
try:
    __import__('rich')
except ImportError:
    import pip
    pip.main(['install', 'rich'])
try:
    __import__('rich.table')
except ImportError:
    import pip
    pip.main(['install', 'rich.table'])
try:
    __import__('hashlib')
except ImportError:
    import pip
    pip.main(['install', 'hashlib'])
try:
    __import__('json')
except ImportError:
    import pip
    pip.main(['install', 'json'])
try:
    __import__('importlib')
except ImportError:
    import pip
    pip.main(['install', 'importlib'])
try:
    __import__('requests')
except ImportError:
    import pip
    pip.main(['install', 'requests'])
try:
    __import__('difflib')
except ImportError:
    import pip
    pip.main(['install', 'difflib'])
try:
    __import__('typing')
except ImportError:
    import pip
    pip.main(['install', 'typing'])
try:
    __import__('uuid')
except ImportError:
    import pip
    pip.main(['install', 'uuid'])
try:
    __import__('queue')
except ImportError:
    import pip
    pip.main(['install', 'queue'])
try:
    __import__('time')
except ImportError:
    import pip
    pip.main(['install', 'time'])

print('All imports have been installed. Press Enter to quit.')
input() 
