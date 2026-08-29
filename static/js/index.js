window.PageLnurlmint = {
  template: '#page-lnurlmint',
  data() {
    return {
      mints: [],
      loading: false,
      errorMessage: '',
      notes: [],
      notesLoading: false,
      activity: [],
      activityLoading: false,
      noteColumns: [
        { name: 'id', label: 'Note ID', field: 'id', align: 'left' },
        { name: 'amount_msat', label: 'Amount', field: 'amount_msat', align: 'right' },
        { name: 'state', label: 'State', field: 'state', align: 'center' }
      ],
      activityColumns: [
        { name: 'type', label: 'Type', field: 'type', align: 'center' },
        { name: 'amount_msat', label: 'Amount', field: 'amount_msat', align: 'right' },
        { name: 'payment_hash', label: 'Payment Hash', field: 'payment_hash', align: 'left' },
        { name: 'settled', label: 'Status', field: 'settled', align: 'center' }
      ],
      createDialog: {
        show: false,
        loading: false,
        data: {
          username: '',
          base_fee_msat: 0,
          fee_percent_ppm: 0,
          min_sendable_msat: 1000,
          max_sendable_msat: 1000000000,
          min_mint_msat: 10000,
          verify_enabled: true,
          sunset_mint: false,
          base_url: '',
          onion_url: ''
        }
      },
      editDialog: {
        show: false,
        loading: false,
        mintId: null,
        data: {
          username: '',
          base_fee_msat: 0,
          fee_percent_ppm: 0,
          min_sendable_msat: 1000,
          max_sendable_msat: 1000000000,
          min_mint_msat: 10000,
          verify_enabled: true,
          sunset_mint: false,
          base_url: '',
          onion_url: ''
        }
      }
    }
  },
  mounted() {
    this.fetchMints()
  },
  methods: {
    openCreateDialog() {
      this.createDialog.data = {
        username: '',
        base_fee_msat: 0,
        fee_percent_ppm: 0,
        min_sendable_msat: 1000,
        max_sendable_msat: 1000000000,
        min_mint_msat: 10000,
        verify_enabled: true,
        sunset_mint: false,
        base_url: '',
        onion_url: ''
      }
      this.createDialog.show = true
    },
    openEditDialog(mint) {
      this.editDialog.mintId = mint.id
      this.editDialog.data = {
        username: mint.username,
        base_fee_msat: mint.base_fee_msat,
        fee_percent_ppm: mint.fee_percent_ppm,
        min_sendable_msat: mint.min_sendable_msat,
        max_sendable_msat: mint.max_sendable_msat,
        min_mint_msat: mint.min_mint_msat,
        verify_enabled: mint.verify_enabled,
        sunset_mint: mint.sunset_mint,
        base_url: mint.base_url || '',
        onion_url: mint.onion_url || ''
      }
      this.editDialog.show = true
    },
    async fetchMints() {
      this.loading = true
      try {
        const wallet = this.g.user.wallets[0]
        const key = wallet.inkey || wallet.adminkey
        const response = await LNbits.api.request(
          'GET',
          '/lnurlmint/api/v1/mints',
          key
        )
        this.mints = response.data || []
      } catch (error) {
        LNbits.utils.notifyApiError(error)
      } finally {
        this.loading = false
      }
    },
    async createMint() {
      this.createDialog.loading = true
      try {
        const wallet = this.g.user.wallets[0]
        await LNbits.api.request(
          'POST',
          '/lnurlmint/api/v1/mints',
          wallet.adminkey,
          this.createDialog.data
        )
        this.createDialog.show = false
        this.fetchMints()
      } catch (error) {
        LNbits.utils.notifyApiError(error)
        this.errorMessage = 'Failed to create mint'
      } finally {
        this.createDialog.loading = false
      }
    },
    async updateMint() {
      this.editDialog.loading = true
      try {
        const wallet = this.g.user.wallets[0]
        await LNbits.api.request(
          'PUT',
          '/lnurlmint/api/v1/mints/' + this.editDialog.mintId,
          wallet.adminkey,
          this.editDialog.data
        )
        this.editDialog.show = false
        this.fetchMints()
      } catch (error) {
        LNbits.utils.notifyApiError(error)
        this.errorMessage = 'Failed to update mint'
      } finally {
        this.editDialog.loading = false
      }
    },
    async deleteMint(mint_id) {
      try {
        const wallet = this.g.user.wallets[0]
        await LNbits.api.request(
          'DELETE',
          '/lnurlmint/api/v1/mints/' + mint_id,
          wallet.adminkey
        )
        this.fetchMints()
      } catch (error) {
        if (error.response && error.response.status === 409) {
          this.errorMessage = 'Cannot delete mint with outstanding notes'
        } else {
          LNbits.utils.notifyApiError(error)
          this.errorMessage = 'Failed to delete mint'
        }
      }
    },
    async fetchNotes(mint_id) {
      this.notesLoading = true
      try {
        const wallet = this.g.user.wallets[0]
        const key = wallet.inkey || wallet.adminkey
        const response = await LNbits.api.request(
          'GET',
          '/lnurlmint/api/v1/mints/' + mint_id + '/notes',
          key
        )
        this.notes = response.data || []
      } catch (error) {
        LNbits.utils.notifyApiError(error)
      } finally {
        this.notesLoading = false
      }
    },
    async fetchActivity(mint_id) {
      this.activityLoading = true
      try {
        const wallet = this.g.user.wallets[0]
        const key = wallet.inkey || wallet.adminkey
        const response = await LNbits.api.request(
          'GET',
          '/lnurlmint/api/v1/mints/' + mint_id + '/activity',
          key
        )
        this.activity = response.data || []
      } catch (error) {
        LNbits.utils.notifyApiError(error)
      } finally {
        this.activityLoading = false
      }
    },
    formatSats(msat) {
      return (msat / 1000).toLocaleString() + ' sats'
    },
    truncateId(id) {
      return id ? id.slice(0, 12) + '...' : ''
    },
    noteState(note) {
      if (note.spent) return 'spent'
      if (note.pending) return 'pending'
      return 'outstanding'
    },
    noteStateColor(note) {
      if (note.spent) return 'grey'
      if (note.pending) return 'orange'
      return 'green'
    }
  }
}
