import hashlib
import re
import unicodedata


RCM_FIELD_OPTION_LABELS = {
    'falla_funcional': 'Falla funcional',
    'modo_de_falla': 'Modo de falla',
    'efecto': 'Efecto',
}


def normalize_rcm_field_option(value):
    text = unicodedata.normalize('NFKD', str(value or '').strip().casefold())
    text = ''.join(char for char in text if not unicodedata.combining(char))
    return re.sub(r'\s+', ' ', text)


def rcm_field_option_key(value):
    normalized = normalize_rcm_field_option(value)
    return hashlib.sha256(normalized.encode('utf-8')).hexdigest() if normalized else ''
