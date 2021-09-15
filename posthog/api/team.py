from typing import Any, Dict, Optional, Type, cast

from django.conf import settings
from django.db import transaction
from django.shortcuts import get_object_or_404
from rest_framework import exceptions, permissions, request, response, serializers, viewsets
from rest_framework.decorators import action

from posthog.api.shared import TeamBasicSerializer
from posthog.constants import AvailableFeature
from posthog.mixins import AnalyticsDestroyModelMixin
from posthog.models import Organization, Team
from posthog.models.organization import OrganizationMembership
from posthog.models.user import User
from posthog.models.utils import generate_random_token, generate_random_token_project
from posthog.permissions import CREATE_METHODS, OrganizationAdminWritePermissions, ProjectMembershipNecessaryPermissions


class PremiumMultiprojectPermissions(permissions.BasePermission):
    """Require user to have all necessary premium features on their plan for create access to the endpoint."""

    message = "You must upgrade your PostHog plan to be able to create and manage multiple projects."

    def has_permission(self, request: request.Request, view) -> bool:
        user = cast(User, request.user)
        if request.method in CREATE_METHODS and (
            (user.organization is None)
            or (
                user.organization.teams.exclude(is_demo=True).count() >= 1
                and not user.organization.is_feature_available(AvailableFeature.ORGANIZATIONS_PROJECTS)
            )
        ):
            return False
        return True


class TeamSerializer(serializers.ModelSerializer):
    effective_membership_level = serializers.SerializerMethodField()

    class Meta:
        model = Team
        fields = (
            "id",
            "uuid",
            "organization",
            "api_token",
            "app_urls",
            "name",
            "slack_incoming_webhook",
            "created_at",
            "updated_at",
            "anonymize_ips",
            "completed_snippet_onboarding",
            "ingested_event",
            "test_account_filters",
            "is_demo",
            "timezone",
            "data_attributes",
            "session_recording_opt_in",
            "session_recording_retention_period_days",
            "effective_membership_level",
        )
        read_only_fields = (
            "id",
            "uuid",
            "organization",
            "api_token",
            "is_demo",
            "created_at",
            "updated_at",
            "ingested_event",
            "effective_membership_level",
        )

    def get_effective_membership_level(self, team: Team) -> Optional[int]:
        requesting_parent_membership: OrganizationMembership = OrganizationMembership.objects.only("id", "level").get(
            organization_id=team.organization_id, user=self.context["request"].user
        )
        if not settings.EE_AVAILABLE:
            # Per-project access not available
            return requesting_parent_membership.level
        from ee.api.explicit_team_member import ExplicitTeamMembership

        try:
            team_membership = ExplicitTeamMembership.objects.only("level").get(
                team=team, parent_membership=requesting_parent_membership
            )
            return max(requesting_parent_membership.level, team_membership.level)
        except ExplicitTeamMembership.DoesNotExist:
            if requesting_parent_membership.level < OrganizationMembership.Level.ADMIN:
                # Only organizations admins and above get implicit project membership
                return None
            return requesting_parent_membership.level

    def create(self, validated_data: Dict[str, Any], **kwargs) -> Team:
        serializers.raise_errors_on_nested_writes("create", self, validated_data)
        request = self.context["request"]
        organization = self.context["view"].organization  # use the org we used to validate permissions
        with transaction.atomic():
            team = Team.objects.create_with_data(**validated_data, organization=organization)
            request.user.current_team = team
            request.user.save()
        return team


class TeamViewSet(AnalyticsDestroyModelMixin, viewsets.ModelViewSet):
    serializer_class = TeamSerializer
    queryset = Team.objects.all()
    permission_classes = [
        permissions.IsAuthenticated,
        ProjectMembershipNecessaryPermissions,
        PremiumMultiprojectPermissions,
    ]
    lookup_field = "id"
    ordering = "-created_by"
    organization: Optional[Organization] = None

    def get_queryset(self):
        # This is actually what ensures that a user cannot read/update a project for which they don't have permission
        return super().get_queryset().filter(organization__in=cast(User, self.request.user).organizations.all())

    def get_serializer_class(self) -> Type[serializers.BaseSerializer]:
        if self.action == "list":
            return TeamBasicSerializer
        return super().get_serializer_class()

    def get_permissions(self):
        """
        Special permissions handling for create requests as the organization is inferred from the current user.
        """
        if self.request.method == "POST" or self.request.method == "DELETE":
            organization = getattr(self.request.user, "organization", None)

            if not organization:
                raise exceptions.ValidationError("You need to belong to an organization.")
            self.organization = (
                organization  # to be used later by `OrganizationAdminWritePermissions` and `TeamSerializer`
            )

            return [
                permission()
                for permission in [
                    permissions.IsAuthenticated,
                    PremiumMultiprojectPermissions,
                    OrganizationAdminWritePermissions,  # Using current org so we don't need to validate membership
                ]
            ]

        return super().get_permissions()

    def get_object(self):
        lookup_value = self.kwargs[self.lookup_field]
        if lookup_value == "@current":
            team = getattr(self.request.user, "team", None)
            if team is None:
                raise exceptions.NotFound("Current project not found.")
            return team
        queryset = self.filter_queryset(self.get_queryset())
        filter_kwargs = {self.lookup_field: lookup_value}
        try:
            team = get_object_or_404(queryset, **filter_kwargs)
        except ValueError as error:
            raise exceptions.ValidationError(str(error))
        self.check_object_permissions(self.request, team)
        return team

    @action(methods=["PATCH"], detail=True)
    def reset_token(self, request: request.Request, id: str, **kwargs) -> response.Response:
        team = self.get_object()
        team.api_token = generate_random_token_project()
        team.save()
        return response.Response(TeamSerializer(team).data)
