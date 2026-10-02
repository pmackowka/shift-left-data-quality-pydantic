# Szablon zmiennych. Skopiuj do terraform.tfvars (plik w .gitignore) i uzupełnij.
# Wartości poniżej są przykładowe - żaden z tych identyfikatorów nie istnieje.

project_id      = "shift-left-dq-1a2b3c"
billing_account = "000000-000000-000000"

# Konto organizacyjne: org_id = "123456789012" albo folder_id = "123456789012".
# Konto prywatne (gmail): zostaw oba zakomentowane.
# org_id    = "123456789012"
# folder_id = "123456789012"

# Pierwsze wdrożenie: false, potem docker push, potem true.
deploy_service = false
image_tag      = "local"
