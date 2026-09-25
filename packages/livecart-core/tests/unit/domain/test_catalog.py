import pytest

from livecart_core.domain.catalog import Catalog, CatalogProduct
from livecart_core.domain.codes import NO_VARIANT, ProductCode, Variant
from livecart_core.domain.errors import InvalidValue
from livecart_core.testing.builders import catalog, catalog_product, demo_catalog


def test_a_product_holds_its_code_name_and_variants() -> None:
    product = CatalogProduct(ProductCode("A12"), "Robe fleurie", (Variant("S"), Variant("M")))

    assert product.code == ProductCode("A12")
    assert product.name == "Robe fleurie"
    assert product.variants == (Variant("S"), Variant("M"))
    assert product.has_variants


def test_a_product_without_variants_has_an_empty_tuple() -> None:
    product = CatalogProduct(ProductCode("B07"), "Sac")

    assert product.variants == ()
    assert not product.has_variants


@pytest.mark.parametrize("name", ["", "   "])
def test_a_product_needs_a_name(name: str) -> None:
    with pytest.raises(InvalidValue, match="needs a name"):
        CatalogProduct(ProductCode("B07"), name)


def test_the_empty_variant_is_not_a_variant_label() -> None:
    with pytest.raises(InvalidValue, match="no variant labels"):
        CatalogProduct(ProductCode("B07"), "Sac", (NO_VARIANT,))


@pytest.mark.parametrize("labels", [("M", "M"), ("M", "m"), ("Bleu", "BLEU")])
def test_two_labels_that_differ_only_by_case_are_refused(labels: tuple[str, str]) -> None:
    with pytest.raises(InvalidValue, match="same variant label twice"):
        catalog_product("A12", "Robe", *labels)


def test_the_catalog_finds_a_product_by_code() -> None:
    robe = catalog_product("A12", "Robe", "S")
    sac = catalog_product("B07", "Sac")
    products = catalog(robe, sac)

    assert products.get(ProductCode("A12")) is robe
    assert products.get(ProductCode("C3")) is None
    assert ProductCode("B07") in products
    assert ProductCode("C3") not in products
    assert "B07" not in products
    assert list(products) == [robe, sac]
    assert len(products) == 2


def test_a_code_appears_once_in_a_catalog() -> None:
    with pytest.raises(InvalidValue, match="lists product A12 twice"):
        catalog(catalog_product("A12", "Robe"), catalog_product("A12", "Autre robe"))


def test_catalogs_are_values_whatever_their_index() -> None:
    first = Catalog((catalog_product("A12", "Robe", "S"),))
    second = Catalog((catalog_product("A12", "Robe", "S"),))

    assert first == second
    assert hash(first) == hash(second)
    assert "_by_code" not in repr(first)


def test_an_empty_catalog_is_allowed() -> None:
    assert len(Catalog(())) == 0


def test_the_demo_catalog_covers_every_variant_shape() -> None:
    products = {str(product.code): product for product in demo_catalog()}

    assert list(products) == ["A12", "B07", "C3", "D4", "E15", "Z99"]
    assert [v.value for v in products["A12"].variants] == ["S", "M", "L"]
    assert not products["B07"].has_variants
    assert [v.value for v in products["C3"].variants] == ["Rouge", "Bleu", "Bleu nuit", "Noir"]
    assert [v.value for v in products["D4"].variants] == ["36", "38", "40", "42"]
    assert [v.value for v in products["E15"].variants] == ["Unique"]
    assert not products["Z99"].has_variants
