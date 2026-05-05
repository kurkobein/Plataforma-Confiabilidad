from django.contrib.auth.models import User
from django.db.models.signals import post_save
from django.dispatch import receiver

from . import models
from .user_sync import sync_auth_user_from_profile, sync_profile_from_auth_user


@receiver(post_save, sender=User)
def keep_profile_in_sync(sender, instance, **kwargs):
    sync_profile_from_auth_user(instance)


@receiver(post_save, sender=models.Usuario)
def keep_auth_user_in_sync(sender, instance, **kwargs):
    sync_auth_user_from_profile(instance)
