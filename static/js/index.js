window.app = Vue.createApp({
  el: '#vue',
  mixins: [windowMixin],
  delimiters: ['${', '}'],
  data: function () {
    return {
      currencyOptions: ['sat'],
      settingsFormDialog: {
        show: false,
        data: {}
      },

      proxiesFormDialog: {
        show: false,
        data: {
          customer_label: null,
          subdomain: null,
          expires_at: null,
          wallet: null,
          
        }
      },
      proxiesList: [],
      proxiesTable: {
        search: '',
        loading: false,
        columns: [
          {"name": "customer_label", "align": "left", "label": "Name", "field": "customer_label", "sortable": true},
          {"name": "subdomain", "align": "left", "label": "Subdomain", "field": "subdomain", "sortable": true},
          {"name": "remote_port", "align": "left", "label": "Port", "field": "remote_port", "sortable": true},
          {"name": "ssh_user", "align": "left", "label": "Default tunneluser", "field": "ssh_user", "sortable": true},
          {"name": "expires_at", "align": "left", "label": "When tunnel defaults", "field": "expires_at", "sortable": true},
          {"name": "status", "align": "left", "label": "active/expired/paid", "field": "status", "sortable": true},
          {"name": "wallet", "align": "left", "label": "Wallet", "field": "wallet", "sortable": true},
          {"name": "updated_at", "align": "left", "label": "Updated At", "field": "updated_at", "sortable": true},
          {"name": "id", "align": "left", "label": "ID", "field": "id", "sortable": true},
          
        ],
        pagination: {
          sortBy: 'updated_at',
          rowsPerPage: 10,
          page: 1,
          descending: true,
          rowsNumber: 10
        }
      },
      settingsFormDialog: {
        show: false,
        data: {}
      },
      paymentsFormDialog: {
        show: false,
        proxies: {label: 'All Proxies', value: ''},
        data: {}
      },
      paymentsList: [],
      paymentsTable: {
        search: '',
        loading: false,
        columns: [
          {"name": "tunnel_id", "align": "left", "label": "Tunnel ID", "field": "tunnel_id", "sortable": true},
          {"name": "invoice_id", "align": "left", "label": "Invoice ID", "field": "invoice_id", "sortable": true},
          {"name": "amount", "align": "left", "label": "Amount", "field": "amount", "sortable": true},
          {"name": "paid", "align": "left", "label": "Paid", "field": "paid", "sortable": true},
          {"name": "updated_at", "align": "left", "label": "Updated At", "field": "updated_at", "sortable": true},
          {"name": "id", "align": "left", "label": "ID", "field": "id", "sortable": true},
          
        ],
        pagination: {
          sortBy: 'updated_at',
          rowsPerPage: 10,
          page: 1,
          descending: true,
          rowsNumber: 10
        }
      }
    }
  },
  watch: {
    'proxiesTable.search': {
      handler() {
        const props = {}
        if (this.proxiesTable.search) {
          props['search'] = this.proxiesTable.search
        }
        this.getProxies()
      }
    },
    'paymentsTable.search': {
      handler() {
        const props = {}
        if (this.paymentsTable.search) {
          props['search'] = this.paymentsTable.search
        }
        this.getPayments()
      }
    },
    'paymentsFormDialog.proxies.value': {
      handler() {
        const props = {}
        if (this.paymentsTable.search) {
          props['search'] = this.paymentsTable.search
        }
        this.getPayments()
      }
    }
  },

  methods: {
//////////////// Settings ////////////////////////
    async updateSettings() {
      
      try {
        const data = {...this.settingsFormDialog.data}

        await LNbits.api.request(
          'PUT',
          '/wallet/api/v1/settings',
          null,
          data
        )
        this.settingsFormDialog.show = false
      } catch (error) {
        LNbits.utils.notifyApiError(error)
      }
    },
    async getSettings() {
      
      try {
        const {data} = await LNbits.api.request(
          'GET',
          '/wallet/api/v1/settings',
          null
        )
        this.settingsFormDialog.data = data
      } catch (error) {
        LNbits.utils.notifyApiError(error)
      }
    },
    async showSettingsDataForm() {
      await this.getSettings()
      this.settingsFormDialog.show = true
    },
    //////////////// Proxies ////////////////////////
    async showNewProxiesForm() {
      this.proxiesFormDialog.data = {
          customer_label: null,
          subdomain: null,
          expires_at: null,
          wallet: null,
          
      }
      this.proxiesFormDialog.show = true
    },
    async showEditProxiesForm(data) {
      this.proxiesFormDialog.data = {...data}
      this.proxiesFormDialog.show = true
    },
    async saveProxies() {
      
      try {
        const data = {extra: {}, ...this.proxiesFormDialog.data}
        const method = data.id ? 'PUT' : 'POST'
        const entry = data.id ? `/${data.id}` : ''
        await LNbits.api.request(
          method,
          '/reverse_proxy/api/v1/proxies' + entry,
          null,
          data
        )
        this.getProxies()
        this.proxiesFormDialog.show = false
      } catch (error) {
        LNbits.utils.notifyApiError(error)
      }
    },

    async getProxies(props) {
      
      try {
        this.proxiesTable.loading = true
        const params = LNbits.utils.prepareFilterQuery(
          this.proxiesTable,
          props
        )
        const {data} = await LNbits.api.request(
          'GET',
          `/reverse_proxy/api/v1/proxies/paginated?${params}`,
          null
        )
        this.proxiesList = data.data
        this.proxiesTable.pagination.rowsNumber = data.total
      } catch (error) {
        LNbits.utils.notifyApiError(error)
      } finally {
        this.proxiesTable.loading = false
      }
    },
    async deleteProxies(proxiesId) {
      await LNbits.utils
        .confirmDialog('Are you sure you want to delete this Proxies?')
        .onOk(async () => {
          try {
            
            await LNbits.api.request(
              'DELETE',
              '/reverse_proxy/api/v1/proxies/' + proxiesId,
              null
            )
            await this.getProxies()
          } catch (error) {
            LNbits.utils.notifyApiError(error)
          }
        })
    },
    async exportProxiesCSV() {
      await LNbits.utils.exportCSV(
        this.proxiesTable.columns,
        this.proxiesList,
        'proxies_' + new Date().toISOString().slice(0, 10) + '.csv'
      )
    },


    async getPayments(props) {
      
      try {
        this.paymentsTable.loading = true
        let params = LNbits.utils.prepareFilterQuery(
          this.paymentsTable,
          props
        )
        const proxiesId = this.paymentsFormDialog.proxies.value
        if (proxiesId) {
          params += `&proxies_id=${proxiesId}`
        }
        const {data} = await LNbits.api.request(
          'GET',
          `/reverse_proxy/api/v1/payments/paginated?${params}`,
          null
        )
        this.paymentsList = data.data
        this.paymentsTable.pagination.rowsNumber = data.total
      } catch (error) {
        LNbits.utils.notifyApiError(error)
      } finally {
        this.paymentsTable.loading = false
      }
    },
    async deletePayments(paymentsId) {
      await LNbits.utils
        .confirmDialog('Are you sure you want to delete this Payments?')
        .onOk(async () => {
          try {
            
            await LNbits.api.request(
              'DELETE',
              '/reverse_proxy/api/v1/payments/' + paymentsId,
              null
            )
            await this.getPayments()
          } catch (error) {
            LNbits.utils.notifyApiError(error)
          }
        })
    },

    async exportPaymentsCSV() {
      await LNbits.utils.exportCSV(
        this.paymentsTable.columns,
        this.paymentsList,
        'payments_' + new Date().toISOString().slice(0, 10) + '.csv'
      )
    },

    //////////////// Utils ////////////////////////
    dateFromNow(date) {
      return moment(date).fromNow()
    },
    async fetchCurrencies() {
      try {
        const response = await LNbits.api.request('GET', '/api/v1/currencies')
        this.currencyOptions = ['sat', ...response.data]
      } catch (error) {
        LNbits.utils.notifyApiError(error)
      }
    }
  },
  ///////////////////////////////////////////////////
  //////LIFECYCLE FUNCTIONS RUNNING ON PAGE LOAD/////
  ///////////////////////////////////////////////////
  async created() {
    this.getSettings()
    this.fetchCurrencies()
    this.getProxies()
    this.getPayments()
  }
})