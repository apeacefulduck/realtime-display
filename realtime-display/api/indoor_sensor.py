"""Ephemeral indoor sensor state. No database, endpoint, or extra transport."""
import json
import math
from datetime import datetime, timezone
from pathlib import Path

RULES = json.loads(Path(__file__).with_name('indoor_rules.json').read_text(encoding='utf-8'))
STALE_SECONDS = RULES['staleAfterMs'] / 1000

def classify(metric, value):
    for rule in RULES[metric]:
        upper = rule['upper']
        if upper is None or value < upper or (rule['inclusive'] and value == upper):
            return rule['label']

def unavailable(last_updated=None):
    return {'type': 'indoor', 'temperature': None, 'humidity': None,
            'temperatureStatus': None, 'humidityStatus': None,
            'sensorAvailable': False, 'lastUpdated': last_updated,
            'staleAfterMs': RULES['staleAfterMs']}

def normalize(payload):
    temperature, humidity = payload.get('temperature'), payload.get('humidity')
    valid = payload.get('sensorAvailable') is True and all(
        type(value) in (int, float) and math.isfinite(value)
        for value in (temperature, humidity))
    # Bound corrupt or incorrectly decoded DHT11 samples.
    valid = valid and -20 <= temperature <= 60 and 0 <= humidity <= 100
    if not valid:
        return unavailable()
    temperature, humidity = round(temperature, 1), round(humidity, 1)
    return {'type': 'indoor', 'temperature': temperature, 'humidity': humidity,
            'temperatureStatus': classify('temperature', temperature),
            'humidityStatus': classify('humidity', humidity), 'sensorAvailable': True,
            # Receipt time is authoritative even before device NTP has synchronized.
            'lastUpdated': datetime.now(timezone.utc).isoformat(),
            'staleAfterMs': RULES['staleAfterMs']}
