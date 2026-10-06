// Generated from api/indoor_rules.json.
const indoorRules = {
  "readIntervalMs": 3000,
  "staleAfterMs": 12000,
  "temperature": [
    {
      "upper": 18,
      "inclusive": false,
      "label": "Soğuk",
      "description": "18 °C'nin altı"
    },
    {
      "upper": 20,
      "inclusive": false,
      "label": "Serin",
      "description": "18–20 °C"
    },
    {
      "upper": 24,
      "inclusive": false,
      "label": "Konforlu",
      "description": "20–24 °C"
    },
    {
      "upper": 27,
      "inclusive": false,
      "label": "Ilık",
      "description": "24–27 °C"
    },
    {
      "upper": 32,
      "inclusive": true,
      "label": "Sıcak",
      "description": "27–32 °C"
    },
    {
      "upper": null,
      "inclusive": false,
      "label": "Çok sıcak",
      "description": "32 °C üzeri"
    }
  ],
  "humidity": [
    {
      "upper": 30,
      "inclusive": false,
      "label": "Kuru",
      "description": "%30 altı"
    },
    {
      "upper": 60,
      "inclusive": true,
      "label": "Konforlu",
      "description": "%30–60"
    },
    {
      "upper": 70,
      "inclusive": true,
      "label": "Nemli",
      "description": "%60–70"
    },
    {
      "upper": null,
      "inclusive": false,
      "label": "Çok nemli",
      "description": "%70 üzeri"
    }
  ]
};
