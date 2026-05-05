from django.contrib import admin
from .registry import MODEL_REGISTRY

for config in MODEL_REGISTRY.values():
    try:
        admin.site.register(config['model'])
    except admin.sites.AlreadyRegistered:
        pass
