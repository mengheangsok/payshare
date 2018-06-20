# -*- coding: utf-8 -*-
import uuid

from django.contrib.auth.hashers import check_password
from django.contrib.auth.hashers import make_password
from django.contrib.auth.models import User
from django.db import models
from django.db.models import Q
from django.db.models.signals import post_save
from django.db.models.signals import pre_save
from django.dispatch import receiver
from django.utils import timezone
from djmoney.models.fields import MoneyField


DEFAULT_AVATAR_URL = "https://avataaars.io/?avatarStyle=Circle&topType=NoHair&accessoriesType=Blank&facialHairType=Blank&clotheType=ShirtCrewNeck&clotheColor=Black&eyeType=Default&eyebrowType=DefaultNatural&mouthType=Default&skinColor=Light"  # noqa


class PayShareError(Exception):
    pass


class UserNotMemberOfCollectiveError(PayShareError):

    def __init__(self, user, collective):
        message = "{} is not part of collective {}".format(user, collective)
        super(UserNotMemberOfCollectiveError, self).__init__(message)


class LiquidationNeedsTwoDifferentUsersError(PayShareError):

    def __init__(self, user):
        message = "{} cannot be both debtor and creditor".format(user)
        super(LiquidationNeedsTwoDifferentUsersError, self).__init__(message)


class TimestampMixin(models.Model):
    """Add created and modified timestamps to a model."""
    created_at = models.DateTimeField(default=timezone.now)
    modified_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class UserProfile(models.Model):
    """A model to attach additional data to a Django User."""

    user = models.OneToOneField("auth.User",
                                on_delete=models.CASCADE,
                                related_name="profile")

    avatar_image_url = models.CharField(max_length=1024,
                                        null=True,
                                        blank=True,
                                        default=DEFAULT_AVATAR_URL)

    def __str__(self):
        return u"Profile for {} ".format(self.user)


@receiver(post_save, sender=User)
def create_userprofile_when_user_created(
        sender, instance, created, **kwargs):
    if created:
        UserProfile.objects.create(user=instance)


class Collective(TimestampMixin, models.Model):
    """A collective groups users that want to share payments.

    Its key is used as an identifier e.g. in URLs. Its token is used to
    authenticate as a User for this Collective instead of having to
    provide key and password everytime. The token updates when the
    password is changed.
    """
    name = models.CharField(max_length=100)
    key = models.UUIDField(default=uuid.uuid4, editable=False)
    password = models.CharField(max_length=128)
    token = models.UUIDField(default=uuid.uuid4, editable=False)
    currency_symbol = models.CharField(default="€", max_length=3)

    def save(self, *args, **kwargs):
        """Make sure to save changed password hashes, not as plain text."""
        if not self.id:
            self._set_password(self.password)
        else:
            password_in_db = Collective.objects.get(id=self.id).password
            if password_in_db != self.password:
                self._set_password(self.password)
        return super(Collective, self).save(*args, **kwargs)

    def check_password(self, password):
        return check_password(password, self.password)

    def is_member(self, user):
        try:
            Membership.objects.get(collective=self, member=user)
            return True
        except Membership.DoesNotExist:
            return False

    def add_member(self, user):
        if not self.is_member(user):
            Membership.objects.create(collective=self, member=user)

    @property
    def members(self):
        return User.objects.filter(membership__collective__id=self.id,
                                   is_active=True)

    @property
    def stats(self):
        """Calculate financial status for each member of the Collective.

        Returns:

            {
                'overall_purchased': 603.45,
                'overall_debt': 50.00,
                'member_id_to_balance': {
                    '<member1-id>': -140.23,
                    '<member2-id>': 67.04,
                    ...
                },
            }

        """
        collective = self

        members = collective.members
        num_members = len(members)
        purchases = collective.purchases
        liquidations = collective.liquidations

        overall_purchased = sum([
            float(purchase.price.amount) for purchase in purchases
        ])
        overall_debt = sum([
            float(liquidation.amount.amount) for liquidation in liquidations
        ])
        per_member = float(overall_purchased) / float(num_members)

        member_id_to_balance = {}
        for member in collective.members:
            member_purchased = sum([
                float(purchase.price.amount) for purchase in purchases
                if purchase.buyer == member
            ])

            credit = sum([
                float(liq.amount.amount) for liq in liquidations
                if liq.creditor == member
            ])
            debt = sum([
                float(liq.amount.amount) for liq in liquidations
                if liq.debtor == member
            ])
            has_to_pay = (
                per_member -
                float(member_purchased) -
                float(credit) +
                float(debt)
            )

            balance = has_to_pay * -1
            if balance == 0:  # Remove '-' from the display.
                balance = 0
            member_id_to_balance[member.id] = balance

        sorted_balances = sorted(
            member_id_to_balance.items(),
            key=lambda item: item[1],
            reverse=True)

        stats = {
            "overall_debt": overall_debt,
            "overall_purchased": overall_purchased,
            "sorted_balances": sorted_balances,
        }
        return stats

    @property
    def liquidations(self):
        """Return Liquidations for all current members."""
        members = self.members
        queries = [
            Q(collective=self, deleted=False),
            Q(
                Q(creditor__in=members) |
                Q(debtor__in=members)
            ),
        ]
        return Liquidation.objects.filter(*queries)

    @property
    def purchases(self):
        """Return Purchases for all current members."""
        return Purchase.objects.filter(collective=self,
                                       buyer__in=self.members,
                                       deleted=False)

    def __str__(self):
        return u"{}".format(self.name)

    def _set_password(self, password):
        """Convert plain text password to a salted hash and rotate token."""
        self.password = make_password(password)
        self.token = uuid.uuid4()


class Membership(TimestampMixin, models.Model):
    """A membership is a mapping of a user to a collective."""
    member = models.ForeignKey("auth.User", on_delete=models.CASCADE)
    collective = models.ForeignKey("purchases.Collective",
                                   on_delete=models.CASCADE)

    class Meta:
        unique_together = ("member", "collective")

    def __str__(self):
        return u"{} in {}".format(self.member.username,
                                  self.collective.name)


class Purchase(TimestampMixin, models.Model):
    """A Purchase describes a certain payment of a member of a Collective."""
    name = models.CharField(max_length=100)
    price = MoneyField(max_digits=10,
                       decimal_places=2,
                       default_currency="EUR")
    buyer = models.ForeignKey("auth.User", on_delete=models.CASCADE)
    collective = models.ForeignKey("purchases.Collective",
                                   on_delete=models.CASCADE)
    deleted = models.BooleanField(default=False)

    def __str__(self):
        return u"{} for {} by {} in {}".format(self.price,
                                               self.name,
                                               self.buyer.username,
                                               self.collective.name)

    @property
    def kind(self):
        return "purchase"

    def delete(self):
        self.deleted = True
        self.save()


@receiver(pre_save, sender=Purchase)
def purchase_pre_save_ensure_membership(sender, instance, *args, **kwargs):
    if not instance.collective.is_member(instance.buyer):
        raise UserNotMemberOfCollectiveError(instance.buyer,
                                             instance.collective)


class Liquidation(TimestampMixin, models.Model):
    """A liquidation describes a repayment of one member to another."""
    name = models.CharField(max_length=100)
    amount = MoneyField(max_digits=10,
                        decimal_places=2,
                        default_currency="EUR")
    debtor = models.ForeignKey("auth.User", related_name="debtor",
                               on_delete=models.CASCADE)
    creditor = models.ForeignKey("auth.User", related_name="creditor",
                                 on_delete=models.CASCADE)
    collective = models.ForeignKey("purchases.Collective",
                                   on_delete=models.CASCADE)
    deleted = models.BooleanField(default=False)

    def __str__(self):
        return u"{} from {} to {} in {}".format(self.amount,
                                                self.creditor.username,
                                                self.debtor.username,
                                                self.collective.name)

    @property
    def kind(self):
        return "liquidation"

    def delete(self):
        self.deleted = True
        self.save()


@receiver(pre_save, sender=Liquidation)
def liquidation_pre_save_ensure_constraints(sender, instance, *args, **kwargs):
    if instance.debtor == instance.creditor:
        raise LiquidationNeedsTwoDifferentUsersError(instance.debtor)
    for user in [instance.debtor, instance.creditor]:
        if not instance.collective.is_member(user):
            raise UserNotMemberOfCollectiveError(user, instance.collective)
