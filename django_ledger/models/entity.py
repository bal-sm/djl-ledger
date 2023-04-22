"""
Django Ledger created by Miguel Sanda <msanda@arrobalytics.com>.
Copyright© EDMA Group Inc licensed under the GPLv3 Agreement.

Contributions to this module:
    * Miguel Sanda <msanda@arrobalytics.com>
    * Pranav P Tulshyan ptulshyan77@gmail.com<>

The EntityModel represents the Company, Corporation, Legal Entity, Enterprise or Person that engage and operate as a
business. EntityModels can be created as part of a parent/child model structure to accommodate complex corporate
structures where certain entities may be owned by other entities and may also generate consolidated financial statements.
Another use case of parent/child model structures is the coordination and authorization of inter-company transactions
across multiple related entities. The EntityModel encapsulates all LedgerModel, JournalEntryModel and TransactionModel which is the core structure of
Django Ledger in order to track and produce all financials.

The EntityModel must be assigned an Administrator at creation, and may have optional Managers that will have the ability
to operate on such EntityModel.

EntityModels may also have different financial reporting periods, (also known as fiscal year), which start month is
specified at the time of creation. All key functionality around the Fiscal Year is encapsulated in the
EntityReportMixIn.

"""
from calendar import monthrange
from collections import defaultdict
from datetime import date, datetime
from decimal import Decimal
from random import choices
from string import ascii_lowercase, digits
from typing import Tuple, Union, Optional, List, Dict
from uuid import uuid4, UUID

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models
from django.db.models import Q
from django.urls import reverse
from django.utils.text import slugify
from django.utils.translation import gettext_lazy as _
from treebeard.mp_tree import MP_Node, MP_NodeManager, MP_NodeQuerySet

from django_ledger.io import IOMixIn
from django_ledger.io import roles as roles_module
from django_ledger.models.accounts import AccountModel, AccountModelQuerySet
from django_ledger.models.bank_account import BankAccountModelQuerySet
from django_ledger.models.coa import ChartOfAccountModel, ChartOfAccountModelQuerySet
from django_ledger.models.coa_default import CHART_OF_ACCOUNTS_ROOT_MAP
from django_ledger.models.customer import CustomerModelQueryset, CustomerModel
from django_ledger.models.items import ItemModelQuerySet, ItemTransactionModelQuerySet
from django_ledger.models.ledger import LedgerModel
from django_ledger.models.mixins import CreateUpdateMixIn, SlugNameMixIn, ContactInfoMixIn, LoggingMixIn
from django_ledger.models.unit import EntityUnitModel
from django_ledger.models.utils import lazy_loader
from django_ledger.models.vendor import VendorModelQuerySet, VendorModel

UserModel = get_user_model()

ENTITY_RANDOM_SLUG_SUFFIX = ascii_lowercase + digits


class EntityModelValidationError(ValidationError):
    pass


class EntityModelQuerySet(MP_NodeQuerySet):
    """
    A custom defined EntityModel QuerySet.
    Inherits from the Materialized Path Node QuerySet Class from Django Treebeard.
    """

    def hidden(self):
        """
        A QuerySet of all hidden EntityModel.

        Returns
        -------
        EntityModelQuerySet
            A filtered QuerySet of hidden EntityModels only.
        """
        return self.filter(hidden=True)

    def visible(self):
        """
        A Queryset of all visible EntityModel.

        Returns
        -------
        EntityModelQuerySet
            A filtered QuerySet of visible EntityModels only.
        """
        return self.filter(hidden=False)


class EntityModelManager(MP_NodeManager):
    """
    A custom defined EntityModel Manager. This ModelManager uses the custom defined EntityModelQuerySet as default.
    Inherits from the Materialized Path Node Manager to include the necessary methods to manage tree-like models.
    This Model Manager keeps track and maintains a root/parent/child relationship between Entities for the purposes of
    producing consolidated financial statements.

    Examples
    ________
    >>> user = request.user
    >>> entity_model_qs = EntityModel.objects.for_user(user_model=user)

    """

    def get_queryset(self):
        """Sets the custom queryset as the default."""
        return EntityModelQuerySet(self.model).order_by('path')

    def for_user(self, user_model):
        """
        This QuerySet guarantees that Users do not access or operate on EntityModels that don't have access to.
        This is the recommended initial QuerySet.

        Parameters
        ----------
        user_model
            The Django User Model making the request.

        Returns
        -------
        EntityModelQuerySet
            A filtered QuerySet of EntityModels that the user has access. The user has access to an Entity if:
                1. Is the Administrator.
                2. Is a manager.
        """
        qs = self.get_queryset()
        return qs.filter(
            Q(admin=user_model) |
            Q(managers__in=[user_model])
        )


class EntityReportMixIn:
    """
    This class encapsulates the functionality needed to determine the start and end of all financial periods of an
    EntityModel. At the moment of creation, an EntityModel must be assigned a calendar month which is going to
    determine the start of the Fiscal Year.
    """
    VALID_QUARTERS = list(range(1, 5))
    VALID_MONTHS = list(range(1, 13))

    def get_fy_start_month(self) -> int:
        """
        The fiscal year start month represents the month (as an integer) when the assigned fiscal year of the
        EntityModel starts.

        Returns
        -------
        int
            An integer representing the month that the fiscal year starts.

        Examples
        ________
            * 1 -> January.
            * 4 -> April.
            * 9 -> September.
        """
        # fy: int = getattr(self, 'fy_start_month')

        try:
            fy: int = getattr(self, 'fy_start_month')
        except AttributeError:
            # current object is not an entity, get current entity and fetch its fy_start_month value

            # if current object is a detail view with an object...
            obj = getattr(self, 'object')
            if isinstance(obj, EntityModel):
                entity = obj
            elif isinstance(obj, LedgerModel):
                entity = obj.entity
            elif isinstance(obj, EntityUnitModel):
                entity = obj.entity
            elif isinstance(obj, AccountModel):
                entity = obj.coa_model.entity

            fy: int = getattr(entity, 'fy_start_month')

        return fy

    def validate_quarter(self, quarter: int):
        """
        Validates the quarter as a valid parameter for other functions.
        Makes sure that only integers 1,2,3, or 4 are used to refer to a particular Quarter.
        Prevents injection of invalid values from views into the IOMixIn.

        Parameters
        ----------
        quarter: int
            The quarter number to validate.

        Raises
        ------
        ValidationError
            If quarter is not valid.
        """
        if quarter not in self.VALID_QUARTERS:
            raise ValidationError(f'Specified quarter is not valid: {quarter}')

    def validate_month(self, month: int):
        """
        Validates the month as a valid parameter for other functions.
        Makes sure that only integers between 1 and 12 are used to refer to a particular month.
        Prevents injection of invalid values from views into the IOMixIn.

        Parameters
        ----------
        month: int
            The month number to validate.

        Raises
        ------

        ValidationError
            If month is not valid.
        """
        if month not in self.VALID_MONTHS:
            raise ValidationError(f'Specified month is not valid: {month}')

    def get_fy_start(self, year: int, fy_start_month: Optional[int] = None) -> date:
        """
        The fiscal year start date of the EntityModel, according to its settings.

        Parameters
        ----------
        year: int
            The fiscal year associated with the requested start date.

        fy_start_month: int
            Optional fiscal year month start. If passed, it will override the EntityModel setting.

        Returns
        -------
        date
            The date when the requested EntityModel fiscal year starts.
        """
        if fy_start_month:
            self.validate_month(fy_start_month)
        fy_start_month = self.get_fy_start_month() if not fy_start_month else fy_start_month
        return date(year, fy_start_month, 1)

    def get_fy_end(self, year: int, fy_start_month: int = None) -> date:
        """
        The fiscal year ending date of the EntityModel, according to its settings.

        Parameters
        ----------
        year: int
            The fiscal year associated with the requested end date.

        fy_start_month: int
            Optional fiscal year month start. If passed, it will override the EntityModel setting.

        Returns
        -------
        date
            The date when the requested EntityModel fiscal year ends.
        """
        if fy_start_month:
            self.validate_month(fy_start_month)
        fy_start_month = self.get_fy_start_month() if not fy_start_month else fy_start_month
        ye = year if fy_start_month == 1 else year + 1
        me = 12 if fy_start_month == 1 else fy_start_month - 1
        return date(ye, me, monthrange(ye, me)[1])

    def get_quarter_start(self, year: int, quarter: int, fy_start_month: int = None) -> date:
        """
        The fiscal year quarter starting date of the EntityModel, according to its settings.

        Parameters
        ----------
        year: int
            The fiscal year associated with the requested start date.

        quarter: int
            The quarter number associated with the requested start date.

        fy_start_month: int
            Optional fiscal year month start. If passed, it will override the EntityModel setting.

        Returns
        -------
        date
            The date when the requested EntityModel quarter starts.
        """
        if fy_start_month:
            self.validate_month(fy_start_month)
        fy_start_month = self.get_fy_start_month() if not fy_start_month else fy_start_month
        self.validate_quarter(quarter)
        quarter_month_start = (quarter - 1) * 3 + fy_start_month
        year_start = year
        if quarter_month_start > 12:
            quarter_month_start -= 12
            year_start = year + 1
        return date(year_start, quarter_month_start, 1)

    def get_quarter_end(self, year: int, quarter: int, fy_start_month: int = None) -> date:
        """
        The fiscal year quarter ending date of the EntityModel, according to its settings.

        Parameters
        ----------
        year: int
            The fiscal year associated with the requested end date.

        quarter: int
            The quarter number associated with the requested end date.

        fy_start_month: int
            Optional fiscal year month start. If passed, it will override the EntityModel setting.

        Returns
        -------
        date
            The date when the requested EntityModel quarter ends.
        """
        if fy_start_month:
            self.validate_month(fy_start_month)
        fy_start_month = self.get_fy_start_month() if not fy_start_month else fy_start_month
        self.validate_quarter(quarter)
        quarter_month_end = quarter * 3 + fy_start_month - 1
        year_end = year
        if quarter_month_end > 12:
            quarter_month_end -= 12
            year_end += 1
        return date(year_end, quarter_month_end, monthrange(year_end, quarter_month_end)[1])

    def get_fiscal_year_dates(self, year: int, fy_start_month: int = None) -> Tuple[date, date]:
        """
        Convenience method to get in one shot both, fiscal year start and end dates.

        Parameters
        ----------
        year: int
            The fiscal year associated with the requested start and end date.

        fy_start_month: int
            Optional fiscal year month start. If passed, it will override the EntityModel setting.

        Returns
        -------
        tuple
            Both, the date when the requested EntityModel fiscal year start and end date as a tuple.
            The start date will be first.

        """
        if fy_start_month:
            self.validate_month(fy_start_month)
        sd = self.get_fy_start(year, fy_start_month)
        ed = self.get_fy_end(year, fy_start_month)
        return sd, ed

    def get_fiscal_quarter_dates(self, year: int, quarter: int, fy_start_month: int = None) -> Tuple[date, date]:
        """
        Convenience method to get in one shot both, fiscal year quarter start and end dates.

        Parameters
        ----------
        year: int
            The fiscal year associated with the requested start and end date.

        quarter: int
            The quarter number associated with the requested start and end date.

        fy_start_month: int
            Optional fiscal year month start. If passed, it will override the EntityModel setting.

        Returns
        -------
        tuple
            Both, the date when the requested EntityModel fiscal year quarter start and end date as a tuple.
            The start date will be first.

        """
        if fy_start_month:
            self.validate_month(fy_start_month)
        self.validate_quarter(quarter)
        qs = self.get_quarter_start(year, quarter, fy_start_month)
        qe = self.get_quarter_end(year, quarter, fy_start_month)
        return qs, qe

    def get_fy_for_date(self, dt: Union[date, datetime], as_str: bool = False) -> Union[str, int]:
        """
        Given a known date, returns the EntityModel fiscal year associated with the given date.

        Parameters
        __________

        dt: date
            Date to evaluate.

        as_str: bool
            If True, return date as a string.


        Returns
        _______
        str or date
            Fiscal year as an integer or string, depending on as_str parameter.
        """
        fy_start_month = self.get_fy_start_month()
        if dt.month >= fy_start_month:
            y = dt.year
        else:
            y = dt.year - 1
        if as_str:
            return str(y)
        return y


class EntityModelAbstract(MP_Node,
                          SlugNameMixIn,
                          CreateUpdateMixIn,
                          ContactInfoMixIn,
                          IOMixIn,
                          LoggingMixIn,
                          EntityReportMixIn):
    """
    The base implementation of the EntityModel. The EntityModel represents the Company, Corporation, Legal Entity,
    Enterprise or Person that engage and operate as a business. The base model inherit from the Materialized Path Node
    of the Django Treebeard library. This allows for complex parent/child relationships between Entities to be tracked
    and managed properly.

    The EntityModel also inherits functionality from the following MixIns:

        1. :func:`SlugNameMixIn <django_ledger.models.mixins.SlugNameMixIn>`
        2. :func:`PaymentTermsMixIn <django_ledger.models.mixins.PaymentTermsMixIn>`
        3. :func:`ContactInfoMixIn <django_ledger.models.mixins.ContactInfoMixIn>`
        4. :func:`CreateUpdateMixIn <django_ledger.models.mixins.CreateUpdateMixIn>`
        5. :func:`EntityReportMixIn <django_ledger.models.mixins.EntityReportMixIn>`
        6. :func:`IOMixIn <django_ledger.io.io_mixin.IOMixIn>`


    Attributes
    __________
    uuid : UUID
        This is a unique primary key generated for the table. The default value of this field is uuid4().

    name: str
        The name of the Company, Enterprise, Person, etc. used to identify the Entity.

    admin: UserModel
        The Django UserModel that will be assigned as the administrator of the EntityModel.

    default_coa: ChartOfAccounts
        The default Chart of Accounts Model of the Entity. EntityModel can have multiple Chart of Accounts but only one
        can be assigned as default.

    managers: UserModel
        The Django UserModels that will be assigned as the managers of the EntityModel by the admin.

    hidden: bool
        A flag used to hide the EntityModel from QuerySets. Defaults to False.

    accrual_method: bool
        A flag used to define which method of accounting will be used to produce financial statements.
            * If False, Cash Method of Accounting will be used.
            * If True, Accrual Method of Accounting will be used.

    fy_start_month: int
        An integer that specifies the month that the Fiscal Year starts.

    picture
        The image or logo used to identify the company on reports or UI/UX.
    """

    CASH_METHOD = 'cash'
    ACCRUAL_METHOD = 'accrual'
    FY_MONTHS = [
        (1, _('January')),
        (2, _('February')),
        (3, _('March')),
        (4, _('April')),
        (5, _('May')),
        (6, _('June')),
        (7, _('July')),
        (8, _('August')),
        (9, _('September')),
        (10, _('October')),
        (11, _('November')),
        (12, _('December')),
    ]
    LOGGER_NAME_ATTRIBUTE = 'slug'

    uuid = models.UUIDField(default=uuid4, editable=False, primary_key=True)
    name = models.CharField(max_length=150, verbose_name=_('Entity Name'))
    default_coa = models.OneToOneField('django_ledger.ChartOfAccountModel',
                                       verbose_name=_('Default Chart of Accounts'),
                                       blank=True,
                                       null=True,
                                       on_delete=models.PROTECT)
    admin = models.ForeignKey(UserModel,
                              on_delete=models.CASCADE,
                              related_name='admin_of',
                              verbose_name=_('Admin'))
    managers = models.ManyToManyField(UserModel,
                                      through='EntityManagementModel',
                                      related_name='managed_by',
                                      verbose_name=_('Managers'))

    hidden = models.BooleanField(default=False)
    accrual_method = models.BooleanField(default=False, verbose_name=_('Use Accrual Method'))
    fy_start_month = models.IntegerField(choices=FY_MONTHS, default=1, verbose_name=_('Fiscal Year Start'))
    picture = models.ImageField(blank=True, null=True)
    objects = EntityModelManager.from_queryset(queryset_class=EntityModelQuerySet)()

    node_order_by = ['uuid']

    class Meta:
        abstract = True
        ordering = ['-created']
        verbose_name = _('Entity')
        verbose_name_plural = _('Entities')
        indexes = [
            models.Index(fields=['admin'])
        ]

    def __str__(self):
        return f'EntityModel: {self.name}'

    # ## Logging ###
    def get_logger_name(self):
        return f'EntityModel {self.uuid}'

    # ### ACCRUAL METHODS ######
    def get_accrual_method(self) -> str:
        if self.is_cash_method():
            return self.CASH_METHOD
        return self.ACCRUAL_METHOD

    def is_cash_method(self) -> bool:
        return self.accrual_method is False

    def is_accrual_method(self) -> bool:
        return self.accrual_method is True

    def is_admin_user(self, user_model):
        return user_model.id == self.admin_id

    # #### SLUG GENERATION ###
    @staticmethod
    def generate_slug_from_name(name: str) -> str:
        """
        Uses Django's slugify function to create a valid slug from any given string.

        Parameters
        ----------
        name: str
            The name or string to slugify.

        Returns
        -------
            The slug as a String.
        """
        slug = slugify(name)
        suffix = ''.join(choices(ENTITY_RANDOM_SLUG_SUFFIX, k=8))
        entity_slug = f'{slug}-{suffix}'
        return entity_slug

    def generate_slug(self,
                      commit: bool = False,
                      raise_exception: bool = True,
                      force_update: bool = False) -> str:
        """
        Convenience method to create the EntityModel slug.

        Parameters
        ----------
        force_update: bool
            If True, will update the EntityModel slug.

        raise_exception: bool
            Raises ValidationError if EntityModel already has a slug.

        commit: bool
            If True,
        """
        if not force_update and self.slug:
            if raise_exception:
                raise ValidationError(
                    message=_(f'Cannot replace existing slug {self.slug}. Use force_update=True if needed.')
                )

        self.slug = self.generate_slug_from_name(self.name)

        if commit:
            self.save(update_fields=[
                'slug',
                'updated'
            ])
        return self.slug

    # #### CHART OF ACCOUNTS ####
    def has_default_coa(self) -> bool:
        """
        Determines if the EntityModel instance has a Default CoA.

        Returns
        -------
        bool
            True if EntityModel instance has a Default CoA.
        """
        return self.default_coa_id is not None

    def get_default_coa(self, raise_exception: bool) -> ChartOfAccountModel:
        if not self.default_coa_id:
            if raise_exception:
                raise EntityModelValidationError(f'EntityModel {self.slug} does not have a default CoA')

    def create_chart_of_accounts(self,
                                 assign_as_default: bool = False,
                                 coa_name: Optional[str] = None,
                                 commit: bool = False) -> ChartOfAccountModel:
        """
        Creates a Chart of Accounts for the Entity Model and optionally assign it as the default Chart of Accounts.
        EntityModel must have a default Chart of Accounts before being able to transact.

        Parameters
        ----------
        coa_name: str
            The new CoA name. If not provided will be auto generated based on the EntityModel name.

        commit: bool
            Commits the transaction into the DB. A ChartOfAccountModel will

        assign_as_default: bool
            Assigns the newly created ChartOfAccountModel as the EntityModel default_coa.

        Returns
        -------
        ChartOfAccountModel
            The newly created chart of accounts model.
        """
        # todo: this logic will generate always the same slug...
        if not coa_name:
            coa_name = 'Default CoA'

        chart_of_accounts = ChartOfAccountModel(
            slug=self.slug + ''.join(choices(ENTITY_RANDOM_SLUG_SUFFIX, k=6)) + '-coa',
            name=coa_name,
            entity=self
        )
        chart_of_accounts.clean()
        chart_of_accounts.save()
        chart_of_accounts.configure()

        if assign_as_default:
            self.default_coa = chart_of_accounts
            if commit:
                self.save(update_fields=[
                    'default_coa',
                    'updated'
                ])
        return chart_of_accounts

    def populate_default_coa(self,
                             activate_accounts: bool = False,
                             force: bool = False,
                             ignore_if_default_coa: bool = True,
                             chart_of_accounts: Optional[ChartOfAccountModel] = None,
                             commit: bool = True):

        if not chart_of_accounts:
            if not self.has_default_coa():
                self.create_chart_of_accounts(assign_as_default=True, commit=commit)
            chart_of_accounts: ChartOfAccountModel = self.default_coa

        coa_accounts_qs = chart_of_accounts.accountmodel_set.all()

        # forces evaluation
        len(coa_accounts_qs)

        coa_has_accounts = coa_accounts_qs.not_coa_root().exists()

        if not coa_has_accounts or force:
            root_accounts = coa_accounts_qs.is_coa_root()

            root_maps = {
                root_accounts.get(role__exact=k): [
                    AccountModel(
                        code=a['code'],
                        name=a['name'],
                        role=a['role'],
                        balance_type=a['balance_type'],
                        active=activate_accounts,
                        # coa_model=chart_of_accounts,
                    ) for a in v] for k, v in CHART_OF_ACCOUNTS_ROOT_MAP.items()
            }

            for root_acc, acc_model_list in root_maps.items():
                for i, account_model in enumerate(acc_model_list):
                    account_model.role_default = True if i == 0 else None
                    account_model.clean()
                    chart_of_accounts.create_account(account_model)

        else:
            if not ignore_if_default_coa:
                raise ValidationError(f'Entity {self.name} already has existing accounts. '
                                      'Use force=True to bypass this check')

    def validate_chart_of_accounts_for_entity(self,
                                              coa_model: ChartOfAccountModel,
                                              raise_exception: bool = True) -> bool:
        if coa_model.entity_id == self.uuid:
            return True
        if raise_exception:
            raise EntityModelValidationError(
                f'Invalid ChartOfAccounts model {coa_model.slug} for EntityModel {self.slug}')
        return False

    def validate_account_model_for_coa(self,
                                       account_model: AccountModel,
                                       coa_model: ChartOfAccountModel,
                                       raise_exception: bool = True) -> bool:
        valid = self.validate_chart_of_accounts_for_entity(coa_model, raise_exception=raise_exception)
        if valid and account_model.coa_model_id == coa_model.uuid:
            return True
        if raise_exception:
            raise EntityModelValidationError(
                f'Invalid AccountModel model {account_model.uuid} for EntityModel {self.slug}'
            )
        return False

    def get_all_coa_accounts(self,
                             order_by: Optional[Tuple[str]] = ('code',),
                             active: bool = True) -> Tuple[
        ChartOfAccountModelQuerySet, Dict[ChartOfAccountModel, AccountModelQuerySet]]:

        """
        Fetches all the AccountModels associated with the EntityModel grouped by ChartOfAccountModel.

        Parameters
        ----------
        active: bool
            Selects only active accounts.
        order_by: list of strings.
            Optional list of fields passed to the order_by QuerySet method.

        Returns
        -------
        Tuple: Tuple[ChartOfAccountModelQuerySet, Dict[ChartOfAccountModel, AccountModelQuerySet]
            The ChartOfAccountModelQuerySet and a grouping of AccountModels by ChartOfAccountModel as keys.
        """

        account_model_qs = ChartOfAccountModel.objects.filter(
            entity_id=self.uuid
        ).select_related('entity').prefetch_related('accountmodel_set')

        return account_model_qs, {
            coa_model: coa_model.accountmodel_set.filter(active=active).order_by(*order_by) for coa_model in
            account_model_qs
        }

    # ##### ACCOUNT MANAGEMENT ######

    def get_all_accounts(self, active: bool = True, order_by: Optional[Tuple[str]] = ('code',)) -> AccountModelQuerySet:
        """
        Fetches all AccountModelQuerySet associated with the EntityModel.

        Parameters
        ----------
        active: bool
            Selects only active accounts.
        order_by: list of strings.
            Optional list of fields passed to the order_by QuerySet method.
        Returns
        -------
        AccountModelQuerySet
            The AccountModelQuerySet of the assigned default CoA.
        """

        account_model_qs = AccountModel.objects.filter(
            coa_model__entity__uuid__exact=self.uuid
        ).select_related('coa_model', 'coa_model__entity')

        if active:
            account_model_qs = account_model_qs.active()
        if order_by:
            account_model_qs = account_model_qs.order_by(*order_by)
        return account_model_qs

    def get_coa_accounts(self,
                         coa_model: Optional[Union[ChartOfAccountModel, UUID, str]] = None,
                         active: bool = True,
                         order_by: Optional[Tuple[str]] = ('code',)) -> AccountModelQuerySet:
        """
        Fetches the AccountModelQuerySet for a specific ChartOfAccountModel.

        Parameters
        ----------
        coa_model: ChartOfAccountModel, UUID, str
            The ChartOfAccountsModel UUID, model instance or slug to pull accounts from. If None, will use default CoA.
        active: bool
            Selects only active accounts.
        order_by: list of strings.
            Optional list of fields passed to the order_by QuerySet method.

        Returns
        -------
        AccountModelQuerySet
            The AccountModelQuerySet of the assigned default CoA.
        """

        if not coa_model:
            account_model_qs = self.default_coa.accountmodel_set.all().select_related('coa_model', 'coa_model__entity')
        else:
            account_model_qs = AccountModel.objects.filter(
                coa_model__entity__uuid__exact=self.uuid
            ).select_related('coa_model', 'coa_model__entity')

            if isinstance(coa_model, ChartOfAccountModel):
                self.validate_chart_of_accounts_for_entity(coa_model=coa_model, raise_exception=True)
            if isinstance(coa_model, str):
                account_model_qs = account_model_qs.filter(coa_model__slug__exact=coa_model)
            elif isinstance(coa_model, UUID):
                account_model_qs = account_model_qs.filter(coa_model__uuid__exact=coa_model)

        if active:
            account_model_qs = account_model_qs.active()

        if order_by:
            account_model_qs = account_model_qs.order_by(*order_by)

        return account_model_qs

    def get_default_coa_accounts(self,
                                 active: bool = True,
                                 order_by: Optional[Tuple[str]] = ('code',),
                                 raise_exception: bool = True) -> Optional[AccountModelQuerySet]:
        """
        Fetches the default AccountModelQuerySet.

        Parameters
        ----------
        active: bool
            Selects only active accounts.
        order_by: list of strings.
            Optional list of fields passed to the order_by QuerySet method.
        raise_exception: bool
            Raises EntityModelValidationError if no default_coa found.

        Returns
        -------
        AccountModelQuerySet
            The AccountModelQuerySet of the assigned default CoA.
        """
        if not self.default_coa_id:
            if raise_exception:
                raise EntityModelValidationError(message=_('No default_coa found.'))
            return

        return self.get_coa_accounts(active=active, order_by=order_by)

    def get_accounts_with_codes(self,
                                code_list: Union[str, List[str]],
                                coa_model: Optional[
                                    Union[ChartOfAccountModel, UUID, str]] = None) -> AccountModelQuerySet:
        """
        Fetches the AccountModelQuerySet with provided code list.

        Parameters
        ----------
        coa_model: ChartOfAccountModel, UUID, str
            The ChartOfAccountsModel UUID, model instance or slug to pull accounts from. Uses default Coa if not
            provided.
        code_list: list or str
            Code or list of codes to fetch.

        Returns
        -------
        AccountModelQuerySet
            The requested AccountModelQuerySet with applied code filter.
        """

        if not coa_model:
            account_model_qs = self.get_default_coa_accounts()
        else:
            account_model_qs = self.get_coa_accounts(coa_model=coa_model)

        if isinstance(code_list, str):
            return account_model_qs.filter(code__exact=code_list)
        return account_model_qs.filter(code__in=code_list)

    def get_default_account_for_role(self,
                                     role: str,
                                     coa_model: Optional[ChartOfAccountModel] = None,
                                     account_model_qs: Optional[AccountModelQuerySet] = None) -> AccountModel:
        if not coa_model:
            coa_model = self.default_coa
        else:
            self.validate_chart_of_accounts_for_entity(coa_model)
        account_model = coa_model.accountmodel_set.get(role__exact=role, role_default=True)

    def create_account_model(self,
                             account_model_kwargs: Dict,
                             coa_model: Optional[Union[ChartOfAccountModel, UUID, str]] = None,
                             raise_exception: bool = True) -> Tuple[ChartOfAccountModel, AccountModel]:
        """
        Creates a new AccountModel for the EntityModel.

        Parameters
        ----------
        coa_model: ChartOfAccountModel, UUID, str
            The ChartOfAccountsModel UUID, model instance or slug to pull accounts from. Uses default Coa if not
            provided.
        account_model_kwargs: dict
            A dictionary of kwargs to be used to create the new AccountModel instance.
        raise_exception: bool
            Raises EntityModelValidationError if ChartOfAccountsModel is not valid for the EntityModel instance.

        Returns
        -------
        A tuple of ChartOfAccountModel, AccountModel
            The ChartOfAccountModel and AccountModel instance just created.
        """
        if coa_model:
            if isinstance(coa_model, UUID):
                coa_model = self.chartofaccountsmodel_set.get(uuid__exact=coa_model)
            elif isinstance(coa_model, str):
                coa_model = self.chartofaccountsmodel_set.get(slug__exact=coa_model)
            elif isinstance(coa_model, ChartOfAccountModel):
                self.validate_chart_of_accounts_for_entity(coa_model=coa_model, raise_exception=raise_exception)
        else:
            coa_model = self.default_coa

        account_model = AccountModel(**account_model_kwargs)
        account_model.clean()
        return coa_model, coa_model.create_account(account_model=account_model)

    # ### VENDOR MANAGEMENT ####
    def get_vendors(self, active: bool = True) -> VendorModelQuerySet:
        """
        Fetches the VendorModels associated with the EntityModel instance.

        Parameters
        ----------
        active: bool
            Active VendorModels only. Defaults to True.

        Returns
        -------
        VendorModelQuerySet
            The EntityModel instance VendorModelQuerySet with applied filters.
        """
        vendor_qs = self.vendormodel_set.all().select_related('entity_model')
        if active:
            vendor_qs = vendor_qs.active()
        return vendor_qs

    def get_vendor_by_number(self, vendor_number: str):
        vendor_model_qs = self.get_vendors()
        return vendor_model_qs.get(vendor_number__exact=vendor_number)

    def get_vendor_by_uuid(self, vendor_uuid: Union[str, UUID]):
        vendor_model_qs = self.get_vendors()
        return vendor_model_qs.get(uuid__exact=vendor_uuid)

    def create_vendor_model(self, vendor_model_kwargs: Dict, commit: bool = True) -> VendorModel:
        """
        Creates a new VendorModel associated with the EntityModel instance.

        Parameters
        ----------
        vendor_model_kwargs: dict
            The kwargs to be used for the new VendorModel.
        commit: bool
            Saves the VendorModel instance in the Database.

        Returns
        -------
        VendorModel
        """
        vendor_model = VendorModel(entity_model=self, **vendor_model_kwargs)
        vendor_model.clean()
        if commit:
            vendor_model.save()
        return vendor_model

    # ### CUSTOMER MANAGEMENT ####

    def get_customers(self, active: bool = True) -> CustomerModelQueryset:
        """
        Fetches the CustomerModel associated with the EntityModel instance.

        Parameters
        ----------
        active: bool
            Active CustomerModel only. Defaults to True.

        Returns
        -------
        CustomerModelQueryset
            The EntityModel instance CustomerModelQueryset with applied filters.
        """
        customer_model_qs = self.customermodel_set.all().select_related('entity_model')
        if active:
            customer_model_qs = customer_model_qs.active()
        return customer_model_qs

    def get_customer_by_number(self, customer_number: str):
        customer_model_qs = self.get_customers()
        return customer_model_qs.get(customer_number__exact=customer_number)

    def get_customer_by_uuid(self, customer_uuid: Union[str, UUID]):
        customer_model_qs = self.get_customers()
        return customer_model_qs.get(uuid__exact=customer_uuid)

    def create_customer_model(self, customer_model_kwargs: Dict, commit: bool = True) -> CustomerModel:
        """
        Creates a new CustomerModel associated with the EntityModel instance.

        Parameters
        ----------
        customer_model_kwargs: dict
            The kwargs to be used for the new CustomerModel.
        commit: bool
            Saves the CustomerModel instance in the Database.

        Returns
        -------
        CustomerModel
        """
        customer_model = CustomerModel(entity_model=self, **customer_model_kwargs)
        customer_model.clean()
        if commit:
            customer_model.save()
        return customer_model

    # ### BILL MANAGEMENT ####
    def get_bills(self):
        BillModel = lazy_loader.get_bill_model()
        return BillModel.objects.filter(
            ledger__entity__uuid__exact=self.uuid
        ).select_related('ledger', 'ledger__entity')

    def create_bill_model(self,
                          vendor_model: Union[VendorModel, UUID, str],
                          xref: Optional[str],
                          additional_info: Optional[Dict] = None,
                          ledger_name: Optional[str] = None,
                          coa_model: Optional[Union[ChartOfAccountModel, UUID, str]] = None,
                          cash_account: Optional[AccountModel] = None,
                          prepaid_account: Optional[AccountModel] = None,
                          payable_account: Optional[AccountModel] = None,
                          commit: bool = True):

        BillModel = lazy_loader.get_bill_model()

        if isinstance(vendor_model, VendorModel):
            if not vendor_model.entity_model_id == self.uuid:
                raise EntityModelValidationError(f'VendorModel {vendor_model.uuid} belongs to a different EntityModel.')
        elif isinstance(vendor_model, UUID):
            vendor_model = self.get_vendor_by_uuid(vendor_uuid=vendor_model)
        elif isinstance(vendor_model, str):
            vendor_model = self.get_vendor_by_number(vendor_number=vendor_model)
        else:
            raise EntityModelValidationError('VendorModel must be an instance of VendorModel, UUID or str.')

        account_model_qs = self.get_coa_accounts(coa_model=coa_model, active=True)

        account_model_qs = account_model_qs.with_roles(roles=[
            roles_module.ASSET_CA_CASH,
            roles_module.ASSET_CA_PREPAID,
            roles_module.LIABILITY_CL_ACC_PAYABLE
        ]).is_role_default()

        # evaluates the queryset...
        len(account_model_qs)

        bill_model = BillModel(
            vendor=vendor_model,
            xref=xref,
            additional_info=additional_info,
            cash_account=account_model_qs.get(role=roles_module.ASSET_CA_CASH) if not cash_account else cash_account,
            prepaid_account=account_model_qs.get(
                role=roles_module.ASSET_CA_PREPAID) if not prepaid_account else prepaid_account,
            unearned_account=account_model_qs.get(
                role=roles_module.LIABILITY_CL_ACC_PAYABLE) if not payable_account else payable_account
        )

        _, bill_model = bill_model.configure(entity_slug=self,
                                             bill_desc=ledger_name,
                                             commit=commit,
                                             commit_ledger=commit)

        return bill_model

    # ### INVOICE MANAGEMENT ####
    def get_invoices(self):
        InvoiceModel = lazy_loader.get_invoice_model()
        return InvoiceModel.objects.filter(
            ledger__entity__uuid__exact=self.uuid
        ).select_related('ledger', 'ledger__entity')

    def create_invoice_model(self,
                             customer_model: Union[VendorModel, UUID, str],
                             additional_info: Optional[Dict] = None,
                             ledger_name: Optional[str] = None,
                             coa_model: Optional[Union[ChartOfAccountModel, UUID, str]] = None,
                             cash_account: Optional[AccountModel] = None,
                             prepaid_account: Optional[AccountModel] = None,
                             payable_account: Optional[AccountModel] = None,
                             commit: bool = True):

        InvoiceModel = lazy_loader.get_invoice_model()

        if isinstance(customer_model, CustomerModel):
            if not customer_model.entity_model_id == self.uuid:
                raise EntityModelValidationError(
                    f'CustomerModel {customer_model.uuid} belongs to a different EntityModel.')
        elif isinstance(customer_model, UUID):
            customer_model = self.get_customer_by_uuid(customer_uuid=customer_model)
        elif isinstance(customer_model, str):
            customer_model = self.get_customer_by_number(customer_number=customer_model)
        else:
            raise EntityModelValidationError('CustomerModel must be an instance of CustomerModel, UUID or str.')

        account_model_qs = self.get_coa_accounts(coa_model=coa_model, active=True)

        account_model_qs = account_model_qs.with_roles(roles=[
            roles_module.ASSET_CA_CASH,
            roles_module.ASSET_CA_PREPAID,
            roles_module.LIABILITY_CL_ACC_PAYABLE
        ]).is_role_default()

        # evaluates the queryset...
        len(account_model_qs)

        invoice_model = InvoiceModel(
            vendor=customer_model,
            additional_info=additional_info,
            cash_account=account_model_qs.get(role=roles_module.ASSET_CA_CASH) if not cash_account else cash_account,
            prepaid_account=account_model_qs.get(
                role=roles_module.ASSET_CA_PREPAID) if not prepaid_account else prepaid_account,
            unearned_account=account_model_qs.get(
                role=roles_module.LIABILITY_CL_ACC_PAYABLE) if not payable_account else payable_account
        )

        _, invoice_model = invoice_model.configure(entity_slug=self,
                                                   bill_desc=ledger_name,
                                                   commit=commit,
                                                   commit_ledger=commit)

        return invoice_model

    # ### PURCHASE ORDER MANAGEMENT ####
    def get_purchase_orders(self):
        return self.purchaseordermodel_set.all().select_related('entity')

    # ### ESTIMATE MODEL MANAGEMENT ####
    def get_estimate_models(self):
        return self.estimatemodel_set.all().select_related('entity')

    # ### BANK ACCOUNT MODEL MANAGEMENT ###
    def get_bank_account_models(self, active: bool = True) -> BankAccountModelQuerySet:
        bank_account_qs = self.bankaccountmodel_set.all().select_related('entity_model')
        if active:
            bank_account_qs = bank_account_qs.active()
        return bank_account_qs

    # ##### INVENTORY MANAGEMENT ####
    @staticmethod
    def inventory_adjustment(counted_qs, recorded_qs) -> defaultdict:
        """
        Computes the necessary inventory adjustment to update balance sheet.

        Parameters
        ----------
        counted_qs: ItemTransactionModelQuerySet
            Inventory recount queryset from Purchase Order received inventory.
            See :func:`ItemTransactionModelManager.inventory_count
            <django_ledger.models.item.ItemTransactionModelManager.inventory_count>`.
            Expects ItemTransactionModelQuerySet to be formatted "as values".

        recorded_qs: ItemModelQuerySet
            Inventory received currently recorded for each inventory item.
            See :func:`ItemTransactionModelManager.inventory_count
            <django_ledger.models.item.ItemTransactionModelManager.inventory_count>`
            Expects ItemModelQuerySet to be formatted "as values".

        Returns
        -------
        defaultdict
            A dictionary with necessary adjustments with keys as tuple:
                0. item_model_id
                1. item_model__name
                2. item_model__uom__name
        """
        counted_map = {
            (i['item_model_id'], i['item_model__name'], i['item_model__uom__name']): {
                'count': i['quantity_onhand'],
                'value': i['value_onhand'],
                'avg_cost': i['cost_average']
                if i['quantity_onhand'] else Decimal('0.00')
            } for i in counted_qs
        }
        recorded_map = {
            (i['uuid'], i['name'], i['uom__name']): {
                'count': i['inventory_received'] or Decimal.from_float(0.0),
                'value': i['inventory_received_value'] or Decimal.from_float(0.0),
                'avg_cost': i['inventory_received_value'] / i['inventory_received']
                if i['inventory_received'] else Decimal('0.00')
            } for i in recorded_qs
        }

        # todo: change this to use a groupby then sum...
        item_ids = list(set(list(counted_map.keys()) + list(recorded_map)))
        adjustment = defaultdict(lambda: {
            # keeps track of inventory recounts...
            'counted': Decimal('0.000'),
            'counted_value': Decimal('0.00'),
            'counted_avg_cost': Decimal('0.00'),

            # keeps track of inventory level...
            'recorded': Decimal('0.000'),
            'recorded_value': Decimal('0.00'),
            'recorded_avg_cost': Decimal('0.00'),

            # keeps track of necessary inventory adjustment...
            'count_diff': Decimal('0.000'),
            'value_diff': Decimal('0.00'),
            'avg_cost_diff': Decimal('0.00')
        })

        for uid in item_ids:

            count_data = counted_map.get(uid)
            if count_data:
                avg_cost = count_data['value'] / count_data['count'] if count_data['count'] else Decimal('0.000')

                adjustment[uid]['counted'] = count_data['count']
                adjustment[uid]['counted_value'] = count_data['value']
                adjustment[uid]['counted_avg_cost'] = avg_cost

                adjustment[uid]['count_diff'] += count_data['count']
                adjustment[uid]['value_diff'] += count_data['value']
                adjustment[uid]['avg_cost_diff'] += avg_cost

            recorded_data = recorded_map.get(uid)
            if recorded_data:
                counted = recorded_data['count']
                avg_cost = recorded_data['value'] / counted if recorded_data['count'] else Decimal('0.000')

                adjustment[uid]['recorded'] = counted
                adjustment[uid]['recorded_value'] = recorded_data['value']
                adjustment[uid]['recorded_avg_cost'] = avg_cost

                adjustment[uid]['count_diff'] -= counted
                adjustment[uid]['value_diff'] -= recorded_data['value']
                adjustment[uid]['avg_cost_diff'] -= avg_cost

        return adjustment

    def update_inventory(self,
                         user_model,
                         commit: bool = False) -> Tuple[defaultdict, ItemTransactionModelQuerySet, ItemModelQuerySet]:
        """
        Triggers an inventory recount with optional commitment of transaction.

        Parameters
        ----------
        user_model: UserModel
            The Django UserModel making the request.

        commit:
            Updates all inventory ItemModels with the new inventory count.

        Returns
        -------
        Tuple[defaultdict, ItemTransactionModelQuerySet, ItemModelQuerySet]
            Return a tuple as follows:
                0. All necessary inventory adjustments as a dictionary.
                1. The recounted inventory.
                2. The recorded inventory on Balance Sheet.
        """
        ItemTransactionModel = lazy_loader.get_item_transaction_model()
        ItemModel = lazy_loader.get_item_model()

        counted_qs: ItemTransactionModelQuerySet = ItemTransactionModel.objects.inventory_count(
            entity_slug=self.slug,
            user_model=user_model
        )
        recorded_qs: ItemModelQuerySet = self.recorded_inventory(user_model=user_model, as_values=False)
        recorded_qs_values = self.recorded_inventory(
            user_model=user_model,
            item_qs=recorded_qs,
            as_values=True)

        adj = self.inventory_adjustment(counted_qs, recorded_qs_values)

        updated_items = list()
        for (uuid, name, uom), i in adj.items():
            item_model: ItemModel = recorded_qs.get(uuid__exact=uuid)
            item_model.inventory_received = i['counted']
            item_model.inventory_received_value = i['counted_value']
            item_model.clean()
            updated_items.append(item_model)

        if commit:
            ItemModel.objects.bulk_update(updated_items,
                                          fields=[
                                              'inventory_received',
                                              'inventory_received_value',
                                              'updated'
                                          ])

        return adj, counted_qs, recorded_qs

    def recorded_inventory(self,
                           user_model,
                           item_qs: Optional[ItemModelQuerySet] = None,
                           as_values: bool = True) -> ItemModelQuerySet:
        """
        Recorded inventory on the books marked as received. PurchaseOrderModel drives the ordering and receiving of
        inventory. Once inventory is marked as "received" recorded inventory of each item is updated by calling
        :func:`update_inventory <django_ledger.models.entity.EntityModelAbstract.update_inventory>`.
        This function returns relevant values of the recoded inventory, including Unit of Measures.

        Parameters
        ----------
        user_model: UserModel
            The Django UserModel making the request.

        item_qs: ItemModelQuerySet
            Pre fetched ItemModelQuerySet. Avoids additional DB Query.

        as_values: bool
            Returns a list of dictionaries by calling the Django values() QuerySet function.


        Returns
        -------
        ItemModelQuerySet
            The ItemModelQuerySet containing inventory ItemModels with additional Unit of Measure information.

        """
        if not item_qs:
            recorded_qs = self.itemmodel_set.inventory_all(
                entity_slug=self.slug,
                user_model=user_model
            )
        else:
            recorded_qs = item_qs
        if as_values:
            return recorded_qs.values(
                'uuid', 'name', 'uom__name', 'inventory_received', 'inventory_received_value')
        return recorded_qs

    def add_equity(self,
                   user_model,
                   cash_account: Union[str, AccountModel],
                   equity_account: Union[str, AccountModel],
                   txs_date: Union[date, str],
                   amount: Decimal,
                   ledger_name: str,
                   ledger_posted: bool = False,
                   je_posted: bool = False):

        if not isinstance(cash_account, AccountModel) and not isinstance(equity_account, AccountModel):

            account_qs = AccountModel.objects.with_roles(
                roles=[
                    roles_module.EQUITY_CAPITAL,
                    roles_module.EQUITY_COMMON_STOCK,
                    roles_module.EQUITY_PREFERRED_STOCK,
                    roles_module.ASSET_CA_CASH
                ],
                entity_slug=self.slug,
                user_model=user_model
            )

            cash_account_model = account_qs.get(code__exact=cash_account)
            equity_account_model = account_qs.get(code__exact=equity_account)

        elif isinstance(cash_account, AccountModel) and isinstance(equity_account, AccountModel):
            cash_account_model = cash_account
            equity_account_model = equity_account

        else:
            raise ValidationError(
                message=f'Both cash_account and equity account must be an instance of str or AccountMode.'
                        f' Got. Cash Account: {cash_account.__class__.__name__} and '
                        f'Equity Account: {equity_account.__class__.__name__}'
            )

        txs = list()
        txs.append({
            'account_id': cash_account_model.uuid,
            'tx_type': 'debit',
            'amount': amount,
            'description': f'Sample data for {self.name}'
        })
        txs.append({
            'account_id': equity_account_model.uuid,
            'tx_type': 'credit',
            'amount': amount,
            'description': f'Sample data for {self.name}'
        })

        # pylint: disable=no-member
        ledger = self.ledgermodel_set.create(
            name=ledger_name,
            posted=ledger_posted
        )

        # todo: this needs to be changes to use the JournalEntryModel API for validation...
        self.commit_txs(
            je_date=txs_date,
            je_txs=txs,
            je_posted=je_posted,
            je_ledger=ledger
        )
        return ledger

    # URLS ----
    def get_dashboard_url(self) -> str:
        """
        The EntityModel Dashboard URL.

        Returns
        _______
        str
            EntityModel dashboard URL as a string.
        """
        return reverse('django_ledger:entity-dashboard',
                       kwargs={
                           'entity_slug': self.slug
                       })

    def get_manage_url(self) -> str:
        """
        The EntityModel Manage URL.

        Returns
        _______
        str
            EntityModel manage URL as a string.
        """
        return reverse('django_ledger:entity-update',
                       kwargs={
                           'entity_slug': self.slug
                       })

    def get_ledgers_url(self) -> str:
        """
        The EntityModel Ledger List URL.

        Returns
        _______
        str
            EntityModel ledger list URL as a string.
        """
        return reverse('django_ledger:ledger-list',
                       kwargs={
                           'entity_slug': self.slug
                       })

    def get_bills_url(self) -> str:
        """
        The EntityModel bill list URL.

        Returns
        _______
        str
            EntityModel bill list URL as a string.
        """
        return reverse('django_ledger:bill-list',
                       kwargs={
                           'entity_slug': self.slug
                       })

    def get_invoices_url(self) -> str:
        """
        The EntityModel invoice list URL.

        Returns
        _______
        str
            EntityModel invoice list URL as a string.
        """
        return reverse('django_ledger:invoice-list',
                       kwargs={
                           'entity_slug': self.slug
                       })

    def get_banks_url(self) -> str:
        """
        The EntityModel bank account list URL.

        Returns
        _______
        str
            EntityModel bank account list URL as a string.
        """
        return reverse('django_ledger:bank-account-list',
                       kwargs={
                           'entity_slug': self.slug
                       })

    def get_balance_sheet_url(self) -> str:
        """
        The EntityModel Balance Sheet Statement URL.

        Returns
        _______
        str
            EntityModel Balance Sheet Statement URL as a string.
        """
        return reverse('django_ledger:entity-bs',
                       kwargs={
                           'entity_slug': self.slug
                       })

    def get_income_statement_url(self) -> str:
        """
        The EntityModel Income Statement URL.

        Returns
        _______
        str
            EntityModel Income Statement URL as a string.
        """
        return reverse('django_ledger:entity-ic',
                       kwargs={
                           'entity_slug': self.slug
                       })

    def get_cashflow_statement_url(self) -> str:
        """
        The EntityModel Cashflow Statement URL.

        Returns
        _______
        str
            EntityModel Cashflow Statement URL as a string.
        """
        return reverse('django_ledger:entity-cf',
                       kwargs={
                           'entity_slug': self.slug
                       })

    def get_data_import_url(self) -> str:
        """
        The EntityModel transaction import URL.

        Returns
        _______
        str
            EntityModel transaction import URL as a string.
        """
        return reverse('django_ledger:data-import-jobs-list',
                       kwargs={
                           'entity_slug': self.slug
                       })

    def get_accounts_url(self) -> str:
        """
        The EntityModel Code of Accounts llist import URL.

        Returns
        _______
        str
            EntityModel Code of Accounts llist import URL as a string.
        """
        return reverse('django_ledger:account-list',
                       kwargs={
                           'entity_slug': self.slug,
                       })

    def get_customers_url(self) -> str:
        """
        The EntityModel customers list URL.

        Returns
        _______
        str
            EntityModel customers list URL as a string.
        """
        return reverse('django_ledger:customer-list',
                       kwargs={
                           'entity_slug': self.slug,
                       })

    def get_vendors_url(self) -> str:
        """
        The EntityModel vendors list URL.

        Returns
        _______
        str
            EntityModel vendors list URL as a string.
        """
        return reverse('django_ledger:vendor-list',
                       kwargs={
                           'entity_slug': self.slug,
                       })

    def get_delete_url(self) -> str:
        """
        The EntityModel delete URL.

        Returns
        _______
        str
            EntityModel delete URL as a string.
        """
        return reverse('django_ledger:entity-delete',
                       kwargs={
                           'entity_slug': self.slug
                       })

    def clean(self):
        if not self.slug:
            self.generate_slug()
        super(EntityModelAbstract, self).clean()


class EntityManagementModelAbstract(CreateUpdateMixIn):
    """
    Entity Management Model responsible for manager permissions to read/write.
    """
    PERMISSIONS = [
        ('read', _('Read Permissions')),
        ('write', _('Read/Write Permissions')),
        ('suspended', _('No Permissions'))
    ]

    uuid = models.UUIDField(default=uuid4, editable=False, primary_key=True)
    entity = models.ForeignKey('django_ledger.EntityModel',
                               on_delete=models.CASCADE,
                               verbose_name=_('Entity'),
                               related_name='entity_permissions')
    user = models.ForeignKey(UserModel,
                             on_delete=models.CASCADE,
                             verbose_name=_('Manager'),
                             related_name='entity_permissions')
    permission_level = models.CharField(max_length=10,
                                        default='read',
                                        choices=PERMISSIONS,
                                        verbose_name=_('Permission Level'))

    class Meta:
        abstract = True
        indexes = [
            models.Index(fields=['entity', 'user']),
            models.Index(fields=['user', 'entity'])
        ]


class EntityStateModelAbstract(models.Model):
    KEY_JOURNAL_ENTRY = 'je'
    KEY_PURCHASE_ORDER = 'po'
    KEY_BILL = 'bill'
    KEY_INVOICE = 'invoice'
    KEY_ESTIMATE = 'estimate'
    KEY_VENDOR = 'vendor'
    KEY_CUSTOMER = 'customer'
    KEY_ITEM = 'item'

    KEY_CHOICES = [
        (KEY_JOURNAL_ENTRY, _('Journal Entry')),
        (KEY_PURCHASE_ORDER, _('Purchase Order')),
        (KEY_BILL, _('Bill')),
        (KEY_INVOICE, _('Invoice')),
        (KEY_ESTIMATE, _('Estimate')),
    ]

    uuid = models.UUIDField(default=uuid4, editable=False, primary_key=True)
    entity_model = models.ForeignKey('django_ledger.EntityModel',
                                     on_delete=models.CASCADE,
                                     verbose_name=_('Entity Model'))
    entity_unit = models.ForeignKey('django_ledger.EntityUnitModel',
                                    on_delete=models.RESTRICT,
                                    verbose_name=_('Entity Unit'),
                                    blank=True,
                                    null=True)
    fiscal_year = models.SmallIntegerField(
        verbose_name=_('Fiscal Year'),
        validators=[MinValueValidator(limit_value=1900)],
        null=True,
        blank=True
    )
    key = models.CharField(choices=KEY_CHOICES, max_length=10)
    sequence = models.BigIntegerField(default=0, validators=[MinValueValidator(limit_value=0)])

    class Meta:
        abstract = True
        indexes = [
            models.Index(fields=['key']),
            models.Index(
                fields=[
                    'entity_model',
                    'fiscal_year',
                    'entity_unit',
                    'key'
                ])
        ]
        unique_together = [
            ('entity_model', 'entity_unit', 'fiscal_year', 'key')
        ]

    def __str__(self):
        return f'{self.__class__.__name__} {self.entity_id}: FY: {self.fiscal_year}, KEY: {self.get_key_display()}'


class EntityStateModel(EntityStateModelAbstract):
    """
    Entity State Model Base Class from Abstract.
    """


class EntityModel(EntityModelAbstract):
    """
    Entity Model Base Class From Abstract
    """


class EntityManagementModel(EntityManagementModelAbstract):
    """
    EntityManagement Model Base Class From Abstract
    """
