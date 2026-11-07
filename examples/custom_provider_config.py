"""Example: Configuring external inference providers with Argus."""

from argus.providers.models import ProviderConfig, PROVIDER_PRESETS
from argus.providers.discovery import resolve_dynamic_tiers

def main() -> None:
    # 1. Inspect supported provider presets
    print("Supported Argus provider presets:")
    for preset_name, preset in PROVIDER_PRESETS.items():
        print(f"  - {preset_name}: {preset.description}")

    # 2. Inspect active routing tiers
    tiers = resolve_dynamic_tiers()
    print("\nActive Dynamic Escalation Ladder:")
    print(f"  T1 (Fast):     {tiers['t1']['model']}")
    print(f"  T2 (Strong):   {tiers['t2']['model']}")
    print(f"  T3 (Frontier): {tiers['t3']['model']}")

if __name__ == "__main__":
    main()
