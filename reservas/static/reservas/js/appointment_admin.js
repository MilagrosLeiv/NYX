document.addEventListener("DOMContentLoaded", function () {
    const salonField = document.getElementById("id_salon");
    const employeeField = document.getElementById("id_employee");

    if (!salonField || !employeeField) return;

    function getSelectedServiceIds() {
        const checkedServices = document.querySelectorAll('input[name="services"]:checked');
        return Array.from(checkedServices).map(input => input.value);
    }

    function clearEmployees() {
        while (employeeField.options.length > 0) {
            employeeField.remove(0);
        }
        employeeField.add(new Option("Seleccioná un profesional", ""));
    }

    function loadEmployees() {
        const salonId = salonField.value;
        const serviceIds = getSelectedServiceIds();
        const currentValue = employeeField.value;

        clearEmployees();

        if (!salonId) return;

        const params = new URLSearchParams();
        params.append("salon_id", salonId);

        serviceIds.forEach(serviceId => {
            params.append("service_ids", serviceId);
        });

        fetch(`/api/employees-by-salon-and-services/?${params.toString()}`)
            .then(response => response.json())
            .then(data => {
                data.employees.forEach(employee => {
                    const option = new Option(employee.name, employee.id);
                    if (String(employee.id) === String(currentValue)) {
                        option.selected = true;
                    }
                    employeeField.add(option);
                });
            })
            .catch(error => {
                console.error("Error cargando profesionales:", error);
            });
    }

    salonField.addEventListener("change", loadEmployees);

    document.querySelectorAll('input[name="services"]').forEach(input => {
        input.addEventListener("change", loadEmployees);
    });

    loadEmployees();
});