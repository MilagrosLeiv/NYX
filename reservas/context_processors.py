def panel_context(request):
    if not request.user.is_authenticated or request.user.is_superuser:
        return {}

    membership = (
        request.user.salon_memberships
        .filter(is_active=True)
        .select_related("salon")
        .first()
    )

    if not membership:
        return {}

    return {
        "salon": membership.salon,
        "panel_role": membership.role,
    }