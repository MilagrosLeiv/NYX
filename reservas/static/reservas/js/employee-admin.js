document.addEventListener("DOMContentLoaded", function () {
    const salonField = document.getElementById("id_salon");
    const servicesFrom = document.getElementById("id_services_from");
    const servicesTo = document.getElementById("id_services_to");

    if (!salonField || !servicesFrom || !servicesTo) return;

    function getOptionsMap(select) {
        const map = new Map();
        Array.from(select.options).forEach(option => {
            map.set(String(option.value), option.text);
        });
        return map;
    }

    function clearSelect(select) {
        while (select.options.length > 0) {
            select.remove(0);
        }
    }

    function loadServices(salonId) {
        const selectedMap = getOptionsMap(servicesTo);

        clearSelect(servicesFrom);
        clearSelect(servicesTo);

        if (!salonId) return;

        fetch(`/api/services-by-salon/?salon_id=${salonId}`)
            .then(response => response.json())
            .then(data => {
                data.services.forEach(service => {
                    const option = new Option(service.name, service.id);

                    if (selectedMap.has(String(service.id))) {
                        servicesTo.add(option);
                    } else {
                        servicesFrom.add(option);
                    }
                });
            })
            .catch(error => {
                console.error("Error cargando servicios:", error);
            });
    }

    salonField.addEventListener("change", function () {
        loadServices(this.value);
    });

    if (salonField.value) {
        loadServices(salonField.value);
    }
});