# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from django.contrib import admin

from payshare.purchases.models import Collective
from payshare.purchases.models import Membership
from payshare.purchases.models import Purchase

# Register your models here.

admin.site.register(Collective)
admin.site.register(Membership)
admin.site.register(Purchase)