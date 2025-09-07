import requests

def get_place_from_coords(coords, place_name=None):
    # Placeholder: usa API geocoding per ottenere info dal nome/coordinate
    lat, lon = coords
    if place_name:
        return f"{place_name} vicino a ({lat}, {lon})"
    return f"Luogo vicino a ({lat}, {lon})"
